# pyreagent

Lab instrument drivers that are safe for AI agents to operate. Each device is
described once in Python, as a typed capability interface. pyreagent builds the
HTTP API, the OpenAPI schema and the MCP tools from that description, and it
enforces the safety rules (locking, limits, human approval, audit) on every call.

## Quick start

```bash
uv sync                    # Python 3.12+
uv run pyreagent demo      # one simulated device of every type on http://127.0.0.1:8000
uv run pytest              # 81 tests, no hardware needed
```

Open <http://127.0.0.1:8000/docs> for the generated API. To let Claude Code drive the bench over MCP:

```bash
claude mcp add --transport http pyreagent http://127.0.0.1:8000/mcp --header "X-Caller: claude-code"
```

## Three layers per device

```
agent / Protocol Studio
        │  HTTP (generated routes)  or  MCP (generated tools)
        ▼
  server/  ──► core/hub.py   DeviceHub: validation, reservations, confirmation gates,
        │                    jobs, idempotency, audit. Both HTTP and MCP go through it.
        ▼
  devices/<type>/<type>.py   Capability interface (frontend): Balance.weigh(), Pump.dispense()…
        │                    Typed signatures + docstrings are what the agent reads.
        ▼
  devices/<type>/<brand>/    Backend (driver): speaks one vendor protocol. Knows nothing about HTTP.
        │
        ▼
  io/                        Transports: serial, TCP, HTTP bridge (ESP32), mock for tests
```

## Layout

```
pyreagent/
  core/            Device, Backend, @capability, DeviceHub, jobs, confirmations, audit, errors
  io/              SerialTransport, TcpTransport, HttpBridge, MockTransport
  devices/
    balance/       balance.py  backend.py  models.py
      simulated/
      mettler_toledo/        MT-SICS, serial or Ethernet
    pump/
      simulated/
      new_era/               NE-1000 family syringe pumps, RS-232
    plate_reader/
      simulated/
    temperature_controller/
      simulated/
    shaker/
      simulated/
    liquid_handler/          liquid_handler.py · deck.py (deck/tip/volume bookkeeping)
      simulated/             records every motion: a dry-run trace of an agent's plan
    thermocycler/            PCR programs: stages x cycles x steps, heated lid
      simulated/
    qpcr/                    extends thermocycler; adds fluorescence + Ct estimate (analysis.py)
      simulated/
    plate_washer/            wash / dispense / prime, buffer and waste checks
      simulated/
  labware/         plates, PCR plates, reservoirs, tip racks: used to validate slot/well/plate arguments
  server/          create_app() (FastAPI) and build_mcp_server() (MCP)
  registry/        registry service + announcer
  configs/         simulated_lab.toml (used by `pyreagent demo`)
examples/bench.toml          real-hardware config
tests/                       includes protocol-level fakes of the MT-SICS balance and NE-1000 pump
_reference/pylabrobot/       read-only reference checkout, git-ignored, not part of pyreagent
```

Each device type has its own folder, with one subfolder per brand. Every type
has a `simulated/` brand, so an agent plan can be tested without hardware or reagents.

## Agent safety

| Concern | What pyreagent does |
|---|---|
| Long operations | `long_running` capabilities return `202` with a job. Poll `GET /jobs/{id}?wait_s=10` (long-poll). `POST /jobs/{id}/cancel` stops the hardware. |
| Two agents at once | Each device has a mutex and an explicit state: `offline`, `idle`, `busy` or `error`. A second mutating call fails fast with `device_busy` and the job id to wait on. `reserve` holds a device across a multi-step sequence. Reads (`read_only`) and safety stops (`interrupt`) always get through. |
| Limits | Schema bounds (`Field(gt=0, le=120)`) plus per-instance bounds (`Within("min_c", "max_c")` against `limits()`), plus a `validate_arguments` hook for things like labware existence. All of them are checked on the server before any human is asked to approve. |
| Errors | Always `{code, message, recoverable, suggested_action, details}`. Faults that leave the hardware state unknown put the device in `error` until a human-approved `reset`. |
| Retries | An `Idempotency-Key` header (or the `idempotency_key` MCP argument) returns the original outcome rather than running the action twice. |
| Human in the loop | Gated capabilities (heater on, tray open, moving reagent) return `428 confirmation_required`. A human approves via `POST /confirmations/{id}/approve`, which needs `X-Operator-Token`, isn't exposed over MCP, and can't be done by the requester. The agent then repeats the identical call with `X-Confirmation-Id`. Approvals are bound to the exact arguments, single-use, and they expire. You can override per device in config (`confirm = { dispense = false }`). |
| Audit | Every call, rejection, job result, approval and reservation is recorded with time, caller, args and outcome. Records go to `GET /audit` and, optionally, a JSONL file. |
| Simulation | Every type has a `simulated` driver. Real drivers are tested against protocol fakes through `MockTransport`. |

## What the newer device types check before asking a human

| Type | Refused before any approval is requested |
|---|---|
| `liquid_handler` | The server tracks the deck: labware per slot, liquid per well, used tips, and what each tip holds. A `transfer` is dry-run against that state, so these are refused up front: not enough liquid in a source, a destination that would overflow, too few compatible tips, an empty slot, the wrong labware kind, below-minimum volumes, and 8-channel positions outside row A. Volumes above the tip size are split into several trips. A `stop` or cancel mid-motion puts the robot in `error` until a human resets it. |
| `thermocycler` / `qpcr` | Every step and the final hold must be within `min_c`..`max_c`. The lid temperature must not exceed `max_lid_c`, and the cycle count must not exceed `max_cycles`. For qPCR, the plate must match the block format, channels must exist, and at least one step must set `acquire`. Runs refuse to start with the lid open. `deactivate` cancels a running program. |
| `plate_washer` | The buffer bottle must exist, the plate format must fit the manifold, columns must be in range, and the volume must fit both the well and the dispenser. Before running, it checks for buffer left and room in the waste bottle (`needs_attention` if not). |

qPCR Ct values are a simple baseline/threshold estimate (`devices/qpcr/analysis.py`). Use the vendor's
software for results you report.

## Registry and discovery

```bash
uv run pyreagent registry --port 9000        # on one machine
# in each device server's config: registry_url + public_url
curl "http://registry:9000/devices?type=pump"
```

Device servers re-announce themselves every `heartbeat_s`. A server that stops
announcing is shown as `stale` and is later dropped. Each device entry carries its
`device_url` and `mcp_url`, so the orchestrator never hard-codes addresses.

## Adding a driver for a new brand

1. Create `pyreagent/devices/<type>/<brand>/` containing `__init__.py` and a driver module.
2. Subclass that type's backend (for example `BalanceBackend`) and implement its abstract methods.
   Take a `transport` argument so tests can inject `MockTransport`.
3. Translate the vendor's status and error replies into `pyreagent.core.errors`.
   Use `NeedsAttention` for physical-setup problems, `HardwareError` when the state is unknown,
   and `InvalidArgument` for out-of-range values.
4. Export `DRIVER = YourBackend` in `__init__.py`. Config can then say `driver = "<brand>"`.
5. Add a protocol fake to `tests/fakes.py` and tests under `tests/drivers/`.

Drivers that live outside this repo also work: set `driver = "my_lab.drivers:MyBackend"`.

## Adding a device type

Create `devices/<type>/` containing `models.py`, `backend.py` (an abstract `Backend` subclass),
`<type>.py` (a `Device` subclass whose methods use `@capability`), and `simulated/`.
Then register the type in `devices/DEVICE_TYPES`. A type can extend another (`Qpcr(Thermocycler)`) and
inherit its capabilities. Checks that need no hardware go in `validate_arguments`, so they run before
approval. Routes and MCP tools are generated
automatically. `tests/test_capabilities.py` fails if a capability or parameter has no
description, or if a gated capability has no hazard text.

## Driver status

| Driver | Status |
|---|---|
| `*/simulated` (all 9 types) | complete |
| `balance/mettler_toledo` (MT-SICS level 0/1: `@ I2 S SI T Z`) | written from the MT-SICS reference and tested against a protocol fake. **Not yet run on a real balance.** |
| `pump/new_era` (NE-1000 basic RS-232 mode) | written from the NE-1000 manual and tested against a protocol fake. **Not yet run on a real pump.** |
