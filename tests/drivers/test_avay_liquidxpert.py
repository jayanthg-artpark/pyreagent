"""Avay LiquidXpert against a fake Marlin controller, checked against G-code run on the instrument."""

from __future__ import annotations

import pytest

from fake_marlin import FakeMarlin
from pyreagent.core import DeviceHub, InvalidArgument
from pyreagent.core.errors import CommunicationError, HardwareError
from pyreagent.devices.liquid_handler import LiquidHandler
from pyreagent.devices.liquid_handler.avay import AvayLiquidXpertBackend, liquidxpert

DECK = {"1": {"labware": "nunc_165305_96_wellplate_300ul", "initial_volume_ul": 0},
        "2": {"labware": "tarsons_t524090_2_reservoir_60ml", "initial_volume_ul": 20000},
        "3": "opentrons_96_tiprack_300ul"}
EJECT = ["M42 P10 S255", "G4 P300", "M42 P10 S0", "G4 P300"]


@pytest.fixture(autouse=True)
def quick_boot(monkeypatch):
    monkeypatch.setattr(liquidxpert, "BOOT_QUIET_S", 0.01)
    monkeypatch.setattr(liquidxpert, "BOOT_TIMEOUT_S", 0.2)


async def start(fake: FakeMarlin, **options) -> DeviceHub:
    backend = AvayLiquidXpertBackend(transport=fake, **options)
    hub = DeviceHub([LiquidHandler("lxp", backend, deck=DECK)])
    await hub.start()
    return hub


async def run(hub: DeviceHub, capability: str, args: dict) -> dict:
    invocation = await hub.invoke("lxp", capability, args)
    if invocation.job is None:
        return invocation.result
    job = await hub.jobs.wait(invocation.job.id, 5)
    assert job.status == "succeeded", job.error
    return job.result


def test_parse_position():
    assert liquidxpert.parse_position("X:89.10 Y:49.30 Z:93.30 A:11.68 Count X:793 Y:1972 Z:18660 A:8970") == (89.1, 49.3, 93.3, 11.68)
    assert liquidxpert.parse_position("echo:busy: processing") is None


async def test_connect_reads_firmware_and_homes_one_axis_at_a_time():
    fake = FakeMarlin()
    hub = await start(fake)
    assert hub.describe_device("lxp")["state"] == "idle", hub.describe_device("lxp")["last_error"]
    assert hub.device("lxp").backend.firmware == "Marlin 2.1.2.7 (Aug 10 2026 12:14:03)"
    assert fake.motion() == ["G0 A6 F500", "G28 A0", "G28 Z0", "G28 Y0", "G28 X0", "G0 Z0 F2200"]
    await hub.stop()


async def test_unpowered_instrument_points_at_the_power_switch():
    fake = FakeMarlin()
    fake.halted = True
    hub = await start(fake)
    error = hub.describe_device("lxp")["last_error"]
    assert error["code"] == "needs_attention" and "switched on" in error["suggested_action"]
    await hub.stop()


async def test_transfer_matches_program_run_on_hardware():
    """The sequence from the instrument run: tips from slot 3, 100 µL from the reservoir into column 1, blow out, discard."""
    fake = FakeMarlin()
    hub = await start(fake)
    fake.written.clear()
    await run(hub, "pick_up_tip", {"slot": "3", "well": "A1"})
    await run(hub, "aspirate", {"slot": "2", "well": "A1", "volume_ul": 100})
    await run(hub, "dispense", {"slot": "1", "well": "A1", "volume_ul": 100})
    await run(hub, "blow_out", {"slot": "1", "well": "A1"})
    await run(hub, "drop_tip", {})
    assert fake.motion() == [
        # tips: column 1 of slot 3
        "G0 Z40 F2200", "G0 A6 F500", "G0 X405.8 Y67.1 F3000", "G0 Z127.8 F2200", "G0 Z40 F2200",
        # aspirate 100 µL, 4 mm above the reservoir bottom on slot 2
        "G0 X258 Y49.4 F3000", "G0 Z82.4 F2200", "G0 Z108.4 F600", "G0 A11.683 F511.5", "G4 P1000", "G0 Z82.4 F600", "G0 Z40 F2200",
        # dispense into column 1 of slot 1, 5 mm above the bottom
        "G0 X70.5 Y67.3 F3000", "G0 Z85.3 F2200", "G0 Z93.3 F600", "G0 A6 F511.5", "G4 P1000", "G0 Z85.3 F600", "G0 Z40 F2200",
        # blow out: back into the wells, plunger past its start, reset once out
        "G0 X70.5 Y67.3 F3000", "G0 Z85.3 F2200", "G0 Z93.3 F600", "G0 A0 F500", "G4 P1000", "G0 Z85.3 F600", "G0 A6 F500",
        "G0 Z40 F2200",
        # discard into the trash
        "G0 X0 Y50 F3000", "G0 Z110 F2200", *EJECT, *EJECT, "G0 Z40 F2200",
    ]
    assert not fake.solenoid_on
    deck = await run(hub, "get_deck", {})
    plate = next(s for s in deck["slots"] if s["slot"] == "1")
    assert plate["volumes_ul"] == {f"{r}1": 100 for r in "ABCDEFGH"}
    await hub.stop()


async def test_every_move_is_confirmed_and_a_lost_step_is_caught():
    fake = FakeMarlin()
    hub = await start(fake)
    fake.written.clear()
    await run(hub, "pick_up_tip", {"slot": "3", "well": "A3"})
    assert "G0 X423.8 Y67.1 F3000" in fake.written  # column 3 is two pitches further
    for index, command in enumerate(fake.written):
        if command.startswith("G0"):
            assert fake.written[index + 1:index + 3] == ["M400", "M114"]
    fake.skew["X"] = 0.5
    with pytest.raises(HardwareError, match="X axis stopped at"):
        await run(hub, "aspirate", {"slot": "2", "well": "A1", "volume_ul": 50})
    await hub.stop()


async def test_flowxpert_calibrations():
    fake = FakeMarlin()
    deck = {"2": {"labware": "tarsons_941196_96_wellplate_300ul", "initial_volume_ul": 100}, "3": "opentrons_96_tiprack_300ul"}
    backend = AvayLiquidXpertBackend(transport=fake)
    hub = DeviceHub([LiquidHandler("lxp", backend, deck=deck)])
    await hub.start()
    await run(hub, "pick_up_tip", {"slot": "3", "well": "A1"})
    fake.written.clear()
    await run(hub, "aspirate", {"slot": "2", "well": "A2", "volume_ul": 50})
    # FlowXpert's position 2 plate: column 1 at X 238.6, row A at Y 130.4; bottom 98.1, touch 87.1
    assert fake.motion()[:3] == ["G0 X247.6 Y67.4 F3000", "G0 Z85.1 F2200", "G0 Z94.1 F600"]
    await hub.stop()


async def test_reservoir_troughs_are_60_mm_apart():
    fake = FakeMarlin()
    hub = await start(fake)
    await run(hub, "pick_up_tip", {"slot": "3", "well": "A1"})
    fake.written.clear()
    await run(hub, "aspirate", {"slot": "2", "well": "A2", "volume_ul": 50})
    assert fake.motion()[0] == "G0 X318 Y49.4 F3000"
    await hub.stop()


async def test_labware_without_a_calibration_is_refused():
    hub = await start(FakeMarlin())
    with pytest.raises(InvalidArgument, match="No calibration for corning_96_wellplate_360ul_flat on slot 2"):
        await run(hub, "load_labware", {"slot": "2", "labware": "corning_96_wellplate_360ul_flat"})
    with pytest.raises(InvalidArgument, match="No calibration for nest_1_reservoir_195ml on slot 1"):
        await run(hub, "load_labware", {"slot": "1", "labware": "nest_1_reservoir_195ml"})
    with pytest.raises(InvalidArgument, match="8 x 12"):
        await run(hub, "load_labware", {"slot": "1", "labware": "corning_384_wellplate_112ul_flat"})
    await hub.stop()

    custom = {"2": {"corning_96_wellplate_360ul_flat": {"x": 250.0, "y": 130.0, "bottom_z": 100.0, "top_z": 88.0}}}
    hub = await start(FakeMarlin(), calibrations=custom)
    await run(hub, "load_labware", {"slot": "2", "labware": "corning_96_wellplate_360ul_flat"})
    await hub.stop()


async def test_plunger_and_height_limits():
    hub = await start(FakeMarlin(), aspirate_height_mm=40)
    await run(hub, "pick_up_tip", {"slot": "3", "well": "A1"})
    with pytest.raises(InvalidArgument, match="depth"):
        await run(hub, "aspirate", {"slot": "2", "well": "A1", "volume_ul": 10})  # the reservoir is 28 mm deep
    await hub.stop()


async def test_unknown_command_is_a_communication_error():
    fake = FakeMarlin()
    hub = await start(fake)
    with pytest.raises(CommunicationError, match="Unknown command"):
        await hub.device("lxp").backend.marlin.send("M999")
    await hub.stop()
