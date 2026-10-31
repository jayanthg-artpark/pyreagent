"""Tecan HydroFlex washer against a fake, with frame bytes captured from a real washer."""

from __future__ import annotations

import pytest

from fake_hydroflex import FakeHydroFlex
from pyreagent.core import DeviceHub, InvalidArgument, NeedsAttention, NotSupported
from pyreagent.core.errors import CommunicationError, DeviceTimeout
from pyreagent.devices.plate_washer import PlateWasher
from pyreagent.devices.plate_washer.tecan import TecanHydroFlexBackend, hydroflex
from pyreagent.io import MockUsbTransport
from pyreagent.vendors.tecan.text import FrameError, encode, take_frame


def test_framing_matches_captured_bytes():
    assert encode("?ALIAS") == bytes.fromhex("02 3F 41 4C 49 41 53 03 00 00 06 68 0D")
    assert encode("INIT FORCE") == bytes.fromhex("02 49 4E 49 54 20 46 4F 52 43 45 03 00 00 0A 66 0D")
    buffer = bytearray(bytes.fromhex("02 42 59 23 54 32 35 30 30 30 03 00 00 09 5A 0D") + bytes.fromhex("02 53 54 03 00 00 02 06 0D"))
    assert take_frame(buffer) == "BY#T25000"
    assert take_frame(buffer) == "ST"
    assert take_frame(buffer) is None
    partial = bytearray(encode("2005007716")[:-1])
    assert take_frame(partial) is None
    with pytest.raises(FrameError):
        take_frame(bytearray(bytes.fromhex("02 53 54 03 00 00 02 07 0D")))


async def start(fake: FakeHydroFlex, **options) -> DeviceHub:
    hub = DeviceHub([PlateWasher("washer", TecanHydroFlexBackend(transport=MockUsbTransport(fake), **options))])
    await hub.start()
    return hub


async def run(hub: DeviceHub, capability: str, args: dict | None = None) -> dict:
    job = await hub.jobs.wait((await hub.invoke("washer", capability, args or {})).job.id, 5)
    assert job.status == "succeeded", job.error
    return job.result


async def test_connect_checks_the_device_and_initialises():
    fake = FakeHydroFlex()
    hub = await start(fake)
    description = hub.describe_device("washer")
    assert description["state"] == "idle", description["last_error"]
    assert fake.commands == ["?DEVICEID", "?VERSION", "INIT FORCE"]
    assert description["limits"]["features"] == ["plate_transport", "stored_programs"]
    assert description["limits"]["buffers"] == ["1", "2", "3", "4"] and description["limits"]["prime_by"] == "time"
    status = (await hub.invoke("washer", "status")).result
    assert status["notes"]["rinse"] == "NO RINSE STARTED" and status["notes"]["firmware"] == "V_4.20_11/17_Hydro_RX"
    assert status["waste_full"] is None
    await hub.stop()


async def test_refused_remote_mode_needs_a_human():
    fake = FakeHydroFlex()
    fake.fail["INIT"] = "ERR25:Remote Mode Not Allowed - Switch Instrument Off/On"
    hub = await start(fake)
    error = hub.describe_device("washer")["last_error"]
    assert error["code"] == "needs_attention" and "switch the washer off and on" in error["suggested_action"]
    await hub.stop()


async def test_plate_transport():
    fake = FakeHydroFlex()
    hub = await start(fake)
    fake.commands.clear()
    await run(hub, "move_plate_out")
    await run(hub, "move_plate_in")
    assert fake.commands == ["?CONFIG OFFSET OUTPOS", "STEPABS MAIN TRANSPORT -960", "STEPABS MAIN TRANSPORT HOME"]
    fake.out_position, fake.commands[:] = "UNKNOWN", []
    with pytest.raises(CommunicationError, match="out position"):
        await hub.invoke("washer", "move_plate_out")
    assert fake.commands == ["?CONFIG OFFSET OUTPOS"]  # nothing moved
    await hub.stop()


async def test_programs():
    fake = FakeHydroFlex()
    fake.split = True  # replies split across USB reads
    hub = await start(fake)
    assert (await hub.invoke("washer", "list_programs")).result == {"1": "ABE", "6": "DISP", "20": "WASH"}  # JSON keys
    program = (await hub.invoke("washer", "get_program", {"program": "disp"})).result
    assert program["number"] == 6 and program["plate"] == 1 and len(program["steps"]) == 2
    fake.commands.clear()
    result = await run(hub, "run_program", {"program": 6})
    assert result["phases"] == ["MSG1: INIT", "MSG8: PRIME", "MSG4: DISPENSE", "MSG2: ASPIRATE"]
    assert fake.commands == ["#PROGRAM", "PROGRAM 6,0,START"]
    fake.commands.clear()
    with pytest.raises(InvalidArgument, match="No program 7"):
        await hub.invoke("washer", "run_program", {"program": 7})
    assert fake.commands == ["#PROGRAM"]  # refused before starting
    await hub.stop()


async def test_program_error_is_structured():
    fake = FakeHydroFlex()
    hub = await start(fake)
    fake.fail["PROGRAM 6"] = "ERR12:Waste Full"
    with pytest.raises(NeedsAttention, match="ERR12:Waste Full"):
        await hub.invoke("washer", "run_program", {"program": "DISP"})
    await hub.stop()


async def test_prime_takes_a_duration():
    fake = FakeHydroFlex()
    hub = await start(fake, channels={"wash": 1, "water": 3})
    fake.commands.clear()
    result = await run(hub, "prime", {"buffer": "water", "duration_s": 10})
    assert fake.commands == ["?RINSE STATUS", "PRIME CHA=3,TIME=10000"]  # supply check, then prime
    assert result["buffer_used_ml"] is None
    with pytest.raises(InvalidArgument, match="give duration_s"):
        await hub.invoke("washer", "prime", {"buffer": "wash"})
    with pytest.raises(InvalidArgument, match="not volume_ml"):
        await hub.invoke("washer", "prime", {"buffer": "wash", "volume_ml": 20})
    with pytest.raises(InvalidArgument):
        await hub.invoke("washer", "prime", {"buffer": "wash", "duration_s": 601})
    await hub.stop()


async def test_parametric_wash_is_not_offered():
    hub = await start(FakeHydroFlex())
    with pytest.raises(NotSupported, match="wash"):
        await hub.invoke("washer", "wash", {"plate": "corning_96_wellplate_360ul", "buffer": "1", "dispense_volume_ul": 300})
    await hub.stop()


async def test_unanswered_and_garbled_replies(monkeypatch):
    monkeypatch.setattr(hydroflex, "QUERY_TIMEOUT_S", 0.05)
    fake = FakeHydroFlex()
    hub = await start(fake)
    backend = hub.device("washer").backend
    with pytest.raises(DeviceTimeout):
        await backend._query("?NOT A COMMAND")
    fake.programs = {}
    assert await backend.list_programs() == {}
    backend.transport._respond = lambda data: [bytes.fromhex("02 53 54 03 00 00 02 07 0D")]
    with pytest.raises(CommunicationError, match="Garbled"):
        await backend._query("?VERSION")
    await hub.stop()


def test_errors():
    assert isinstance(hydroflex.error_for("X", "ERR12:Waste Full"), NeedsAttention)
    assert hydroflex.error_for("X", "ERR3:Motor").code == "hardware_error"
