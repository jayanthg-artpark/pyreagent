"""MCP tools generated from the same capability definitions as the HTTP API.

Every device capability becomes a tool named ``<device>__<capability>``, plus a
few built-in tools for discovery, jobs, confirmations and reservations. Tools
call ``DeviceHub.invoke``, so MCP callers get exactly the HTTP API's checks.
Approving confirmations is deliberately *not* a tool: that is for humans.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import mcp_types as types
from mcp.server.lowlevel import Server
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import __version__
from ..core.capability import CapabilitySpec
from ..core.device import Device
from ..core.errors import InvalidArgument, NotFound, PyreagentError, translate_exception
from ..core.hub import DeviceHub
from ..labware import DEFAULT_CATALOG

log = logging.getLogger("pyreagent.mcp")

DEFAULT_CALLER = "mcp-agent"

INSTRUCTIONS = """\
You control laboratory instruments through this server. Rules:
1. Call list_devices first. Each device lists its state, limits and capability tools (named <device>__<capability>).
2. Respect limits; the server enforces them and rejects anything outside them with code invalid_argument.
3. Long-running tools return status "accepted" with a job. Poll get_job(job_id, wait_s=10) until the job's status is
   succeeded, failed or cancelled. Do not re-issue the command while its job is running.
4. If a call fails with code confirmation_required, tell the human what the action is and its hazard, poll
   get_confirmation until it is approved, then repeat the identical call with confirmation_id set. Never guess approval.
5. Every error has recoverable and suggested_action. Retry only when recoverable is true; otherwise follow
   suggested_action or stop and ask the human.
6. For a multi-step sequence on one device, call reserve_device first and release_device after.
7. Pass a fresh idempotency_key on actions you might retry; a retry with the same key returns the original outcome.
"""


@dataclass
class ToolEntry:
    tool: types.Tool
    handler: Callable[[dict[str, Any], str], Awaitable[Any]]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _NoArgs(_Args):
    pass


class _DeviceArgs(_Args):
    device: str = Field(description="Device name from list_devices.")


class _JobArgs(_Args):
    job_id: str = Field(description="Job id returned by a long-running capability.")
    wait_s: float = Field(default=0, ge=0, le=30, description="Wait up to this many seconds for the job to finish before answering.")


class _ListJobsArgs(_Args):
    device: str | None = Field(default=None, description="Only jobs for this device.")
    limit: int = Field(default=20, ge=1, le=200)


class _ConfirmationArgs(_Args):
    confirmation_id: str = Field(description="Id from a confirmation_required error.")


class _ReserveArgs(_DeviceArgs):
    ttl_s: float = Field(default=300, ge=1, le=3600, description="Reservation lifetime; call again to extend.")


class _AuditArgs(_Args):
    device: str | None = Field(default=None, description="Only records for this device.")
    limit: int = Field(default=20, ge=1, le=200)


def _compact_device(hub: DeviceHub, name: str) -> dict[str, Any]:
    full = hub.describe_device(name)
    return {
        **{k: full[k] for k in ("name", "type", "state", "simulated", "limits", "reservation", "running_jobs", "last_error", "activity")},
        "capabilities": [
            {
                "tool": f"{name}__{c['name']}",
                "summary": c["summary"],
                "long_running": c["long_running"],
                "requires_confirmation": c["requires_confirmation"],
                "read_only": c["read_only"],
            }
            for c in full["capabilities"]
        ],
    }


def _builtin_tools(hub: DeviceHub) -> list[tuple[str, str, type[_Args], Callable[[Any, str], Awaitable[Any]], bool]]:
    async def list_devices(a: _NoArgs, caller: str) -> Any:
        return {"server": hub.name, "devices": [_compact_device(hub, n) for n in hub.devices]}

    async def get_device(a: _DeviceArgs, caller: str) -> Any:
        return hub.describe_device(a.device)

    async def get_job(a: _JobArgs, caller: str) -> Any:
        return (await hub.jobs.wait(a.job_id, a.wait_s)).model_dump(mode="json")

    async def list_jobs(a: _ListJobsArgs, caller: str) -> Any:
        return {"jobs": [j.model_dump(mode="json") for j in hub.jobs.list(device=a.device, limit=a.limit)]}

    async def cancel_job(a: _JobArgs, caller: str) -> Any:
        return (await hub.jobs.cancel(a.job_id)).model_dump(mode="json")

    async def get_confirmation(a: _ConfirmationArgs, caller: str) -> Any:
        return hub.confirmations.get(a.confirmation_id).model_dump(mode="json")

    async def reserve_device(a: _ReserveArgs, caller: str) -> Any:
        return hub.reserve(a.device, caller=caller, ttl_s=a.ttl_s).to_dict()

    async def release_device(a: _DeviceArgs, caller: str) -> Any:
        hub.release(a.device, caller=caller)
        return {"released": a.device}

    async def list_labware(a: _NoArgs, caller: str) -> Any:
        return {"labware": [lw.model_dump() for lw in DEFAULT_CATALOG.list()]}

    async def get_audit_log(a: _AuditArgs, caller: str) -> Any:
        return {"records": [r.model_dump(mode="json") for r in hub.audit.query(device=a.device, limit=a.limit)]}

    return [
        ("list_devices", "List every device on this server with its state, limits and capability tools. Call this first.", _NoArgs, list_devices, True),
        ("get_device", "Full description of one device, including parameter schemas for each capability.", _DeviceArgs, get_device, True),
        ("get_job", "Status, progress and result of a job. Use wait_s to long-poll instead of calling in a tight loop.", _JobArgs, get_job, True),
        ("list_jobs", "Recent jobs, newest first.", _ListJobsArgs, list_jobs, True),
        ("cancel_job", "Cancel a running job. The device is told to stop what it was doing.", _JobArgs, cancel_job, False),
        ("get_confirmation", "Check whether a human has approved or rejected a gated action.", _ConfirmationArgs, get_confirmation, True),
        ("reserve_device", "Reserve a device so other callers cannot send it mutating commands until you release it or the reservation expires.", _ReserveArgs, reserve_device, False),
        ("release_device", "Release a reservation you hold.", _DeviceArgs, release_device, False),
        ("list_labware", "Labware ids the server knows (plates etc.), with their geometry.", _NoArgs, list_labware, True),
        ("get_audit_log", "Recent audit records: who called what, with which arguments, and the outcome.", _AuditArgs, get_audit_log, True),
    ]


def _capability_description(device: Device[Any], spec: CapabilitySpec) -> str:
    meta = spec.meta
    notes = [f"Device: {device.name} ({device.device_type}{', simulated' if device.backend.simulated else ''})."]
    if meta.long_running:
        notes.append("Long-running: returns status 'accepted' with a job; poll get_job until it finishes.")
    if device.requires_confirmation(spec.name):
        notes.append(
            "Needs human approval on every call"
            + (f" ({meta.hazard})" if meta.hazard else "")
            + ". The first call fails with confirmation_required and a confirmation_id; after a human approves, "
            "repeat the identical call with confirmation_id."
        )
    if meta.read_only:
        notes.append("Read-only: allowed at any time.")
    if meta.interrupt:
        notes.append("Safety stop: always allowed, even while the device is busy or reserved by someone else.")
    return f"{meta.description}\n\n" + "\n".join(notes)


def _capability_schema(device: Device[Any], spec: CapabilitySpec) -> dict[str, Any]:
    schema = dict(spec.parameters_schema())
    properties = dict(schema.get("properties", {}))
    properties["idempotency_key"] = {
        "type": "string",
        "description": "Optional. Reuse the same key when retrying this exact action to avoid doing it twice.",
    }
    if device.requires_confirmation(spec.name):
        properties["confirmation_id"] = {
            "type": "string",
            "description": "Id of an approved confirmation for this exact call (from a confirmation_required error).",
        }
    schema["properties"] = properties
    schema.setdefault("type", "object")
    return schema


def build_tools(hub: DeviceHub) -> dict[str, ToolEntry]:
    entries: dict[str, ToolEntry] = {}
    for name, description, model, fn, read_only in _builtin_tools(hub):
        async def handler(args: dict[str, Any], caller: str, _model: type[_Args] = model, _fn: Any = fn) -> Any:
            try:
                parsed = _model.model_validate(args)
            except ValidationError as exc:
                raise InvalidArgument(f"Invalid arguments: {exc.errors(include_url=False)[0]['msg']}",
                                      details={"errors": exc.errors(include_url=False, include_context=False, include_input=False)}) from None
            return await _fn(parsed, caller)

        entries[name] = ToolEntry(
            tool=types.Tool(
                name=name, description=description, input_schema=model.model_json_schema(),
                annotations=types.ToolAnnotations(read_only_hint=read_only),
            ),
            handler=handler,
        )

    for device in hub.devices.values():
        for spec in device.capability_specs().values():
            tool_name = f"{device.name}__{spec.name}"

            async def invoke(args: dict[str, Any], caller: str, _device: str = device.name, _cap: str = spec.name) -> Any:
                args = dict(args)
                confirmation_id = args.pop("confirmation_id", None)
                idempotency_key = args.pop("idempotency_key", None)
                invocation = await hub.invoke(
                    _device, _cap, args, caller=caller, confirmation_id=confirmation_id, idempotency_key=idempotency_key
                )
                return invocation.model_dump(mode="json")

            gated = device.requires_confirmation(spec.name)
            entries[tool_name] = ToolEntry(
                tool=types.Tool(
                    name=tool_name,
                    title=f"{device.name}: {spec.name.replace('_', ' ')}",
                    description=_capability_description(device, spec),
                    input_schema=_capability_schema(device, spec),
                    annotations=types.ToolAnnotations(
                        read_only_hint=spec.meta.read_only,
                        destructive_hint=gated,
                        idempotent_hint=spec.meta.read_only,
                        open_world_hint=False,
                    ),
                ),
                handler=invoke,
            )
    return entries


def _caller(ctx: Any) -> str:
    headers = getattr(getattr(ctx, "request", None), "headers", None)
    if headers is not None:
        value = headers.get("x-caller")
        if value:
            return value
    return DEFAULT_CALLER


def _result(payload: Any, *, is_error: bool = False) -> types.CallToolResult:
    structured = payload if isinstance(payload, dict) else {"result": payload}
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(structured, default=str))],
        structured_content=structured,
        is_error=is_error,
    )


def build_mcp_server(hub: DeviceHub) -> Server[Any]:
    entries = build_tools(hub)

    async def on_list_tools(ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[e.tool for e in entries.values()])

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        entry = entries.get(params.name)
        try:
            if entry is None:
                raise NotFound(f"No tool named {params.name!r}.", suggested_action="Call list_devices to see available tools.")
            return _result(await entry.handler(dict(params.arguments or {}), _caller(ctx)))
        except PyreagentError as err:
            return _result({"error": err.to_dict()}, is_error=True)
        except Exception as exc:  # never let a driver bug kill the MCP session
            log.exception("tool %s failed", params.name)
            return _result({"error": translate_exception(exc).to_dict()}, is_error=True)

    return Server(
        "pyreagent",
        version=__version__,
        title=f"pyreagent: {hub.name}",
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
