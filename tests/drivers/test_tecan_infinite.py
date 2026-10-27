"""Tecan Infinite 200 PRO driver against a protocol fake, using the protocol spec's test vectors."""

from __future__ import annotations

import asyncio
import math
import struct

import pytest

from fake_infinite import ESC_FRAME, FakeInfinite, binary
from pyreagent.core import DeviceHub, InvalidArgument, NeedsAttention, PyreagentError
from pyreagent.devices.plate_reader import PlateReader
from pyreagent.devices.plate_reader.tecan import TecanInfiniteBackend
from pyreagent.devices.plate_reader.tecan.protocol import (
    AbsorbanceDecoder,
    AbsorbanceReference,
    CalibrationError,
    FluorescenceDecoder,
    FrameParser,
    LuminescenceDecoder,
    busy_ms,
    decode_status,
    encode,
    optical_density,
    parse_capability,
)
from pyreagent.labware import DEFAULT_CATALOG, Labware, LabwareCatalog, LabwareGeometry

# The spec's 2 x 3 test plate: wells at x 3/13/23 mm, rows 8/16 mm from the back edge.
PLATE = Labware(id="test_2x3_plate", rows=2, columns=3, geometry=LabwareGeometry(
    size_x_mm=30, size_y_mm=20, size_z_mm=10, a1_x_mm=3, a1_y_mm=12, pitch_x_mm=10, pitch_y_mm=8,
    well_bottom_z_mm=1, well_depth_mm=5))
CATALOG = LabwareCatalog([PLATE, *DEFAULT_CATALOG.list()])
CLEARS = ["TIME CLEAR", "GAIN CLEAR", "READS CLEAR", "POSITION CLEAR", "MIRROR CLEAR"]
END = ["KEYLOCK OFF", "ABSOLUTE MTP,IN"]


def h(text: str) -> bytes:
    return bytes.fromhex(text)


async def start(fake: FakeInfinite | None = None, **options) -> tuple[DeviceHub, FakeInfinite]:
    fake = fake or FakeInfinite()
    hub = DeviceHub([PlateReader("reader", TecanInfiniteBackend(transport=fake, **options), labware=CATALOG, poll_interval_s=0.01)])
    await hub.start()
    assert hub.describe_device("reader")["state"] == "idle", hub.describe_device("reader")["last_error"]
    fake.commands.clear()
    fake.reads.clear()
    return hub, fake


async def run(hub: DeviceHub, capability: str, args: dict | None = None):
    accepted = await hub.invoke("reader", capability, args or {})
    if accepted.job is None:
        return accepted.result
    return await hub.jobs.wait(accepted.job.id, 5)


def timeouts_after(fake: FakeInfinite, command: str) -> list[float]:
    return [t for c, t in fake.reads if c == command]


# -- wire format --------------------------------------------------------------------------------------

def test_framing_vectors():
    assert encode("A") == h("02 41 03 00 00 01 40 0D")
    assert encode("ST") == h("02 53 54 03 00 00 02 06 0D")
    assert encode("+") == h("02 2B 03 00 00 01 2A 0D")
    assert encode("-") == h("02 2D 03 00 00 01 2C 0D")
    assert encode("QQ") == h("02 51 51 03 00 00 02 01 0D")
    assert encode("INIT FORCE") == h("02 49 4E 49 54 20 46 4F 52 43 45 03 00 00 0A 66 0D")
    assert encode("BY#T80000") == h("02 42 59 23 54 38 30 30 30 30 03 00 00 09 55 0D")
    assert encode("BY#T5000") == h("02 42 59 23 54 35 30 30 30 03 00 00 08 68 0D")
    assert encode("ABSOLUTE MTP,OUT") == h("02 41 42 53 4F 4C 55 54 45 20 4D 54 50 2C 4F 55 54 03 00 00 10 1D 0D")
    assert encode("?ABSOLUTE MTP,POS") == h("02 3F 41 42 53 4F 4C 55 54 45 20 4D 54 50 2C 50 4F 53 03 00 00 11 20 0D")
    assert encode("OUT") == h("02 4F 55 54 03 00 00 03 4F 0D")
    assert encode("IN") == h("02 49 4E 03 00 00 02 06 0D")
    assert encode("?TEMPERATURE PLATE,CURRENT") == h(
        "02 3F 54 45 4D 50 45 52 41 54 55 52 45 20 50 4C 41 54 45 2C 43 55 52 52 45 4E 54 03 00 00 1A 7B 0D")
    assert encode("218") == h("02 32 31 38 03 00 00 03 3A 0D")
    assert encode("TEMPERATURE PLATE,TARGET=370") == h(
        "02 54 45 4D 50 45 52 41 54 55 52 45 20 50 4C 41 54 45 2C 54 41 52 47 45 54 3D 33 37 30 03 00 00 1C 15 0D")
    assert encode("SHAKING ON") == h("02 53 48 41 4B 49 4E 47 20 4F 4E 03 00 00 0A 71 0D")
    assert encode("BY#T2000") == h("02 42 59 23 54 32 30 30 30 03 00 00 08 6F 0D")
    assert encode("SCANX 3000,23000,3") == h("02 53 43 41 4E 58 20 33 30 30 30 2C 32 33 30 30 30 2C 33 03 00 00 12 67 0D")
    assert encode("22,BIN:") == h("02 32 32 2C 42 49 4E 3A 03 00 00 07 52 0D")
    assert encode("14,BIN:") == h("02 31 34 2C 42 49 4E 3A 03 00 00 07 57 0D")


def test_parser_splits_frames_binary_and_status_frames():
    calibration = h("00 00 17 70 00 00 00 00 00 00 00 00 00 00 03 E8 00 00 00 00 03 E8")
    data = h("00 00 17 70 00 00 00 00 00 00 01 F4 03 E8")
    stream = encode("BY#T1000") + encode("ST") + ESC_FRAME + binary(calibration) + binary(data) + encode("ST")[:-1]
    whole = FrameParser().feed(stream)
    assert whole == ["BY#T1000", "ST", calibration, data, "ST"]  # the last CR is optional
    parser, items = FrameParser(), []
    for i in range(len(stream)):  # one byte at a time
        items += parser.feed(stream[i:i + 1])
    assert items == whole and not parser.binary_pending
    assert FrameParser().feed(b"\r\nOUT", bare_ascii=False) == []
    assert FrameParser().feed(b"OUT", bare_ascii=True) == ["OUT"]


def test_reply_and_status_decoding():
    assert busy_ms("BY#T80000") == 80000 and busy_ms("+BY#T0") == 0 and busy_ms("ST") is None
    table = {"ST": "standby", "PD": "power_down", "PU": "power_up", "PA": "parked", "BY": "busy", "BY#T5000": "busy",
             "BY%50": "busy", "BY$reading": "busy", "BY#C4": "busy", "+BY#C2": "busy_in_background", "BY%C50": "unknown",
             "MSG001: service requested": "message", "NEW_STATUS": "unknown"}
    assert {reply: decode_status(reply) for reply in table} == table
    assert parse_capability("700|1000", 1) == 700 and parse_capability(" 650.7:x~y", 1) == 650
    assert parse_capability("-", 700) == 700


def test_conversions():
    absorbance = AbsorbanceDecoder()
    assert absorbance.add(h("00 00 17 70 00 00 00 00 00 00 01 F4 03 E8")) == "data"
    assert absorbance.add(h("00 00 17 70 00 00 00 00 00 00 00 00 00 00 03 E8 00 00 00 00 03 E8")) == "calibration"
    assert absorbance.results() == [pytest.approx(0.3010299956639812)]

    fluorescence = FluorescenceDecoder()
    fluorescence.add(h("00 00 12 F2 14 50 00 00 00 00 00 00 01 F4 03 E8"))
    assert fluorescence.results() == [500.0]  # uncorrected without calibration
    fluorescence.add(h("12 F2 00 00 00 00 00 00 00 00 00 64 00 00 00 00 03 E8"))  # meas dark 100
    assert fluorescence.results() == [400.0]

    luminescence = LuminescenceDecoder(dark_s=3, integration_s=1)
    luminescence.add(bytes(10))
    luminescence.add(h("00 00 00 00 00 00 00 00 00 00 00 00 03 E8"))
    assert luminescence.results() == [1000.0]
    luminescence.add(bytes(6) + struct.pack(">i", 300))  # 300 dark counts over 3 s
    assert luminescence.results() == [900.0]

    ref = AbsorbanceReference(meas_dark=0, meas_bright=1000, ref_dark=0, ref_bright=1000)
    assert optical_density([ref], [(0, 1000)]) == pytest.approx(4.0)  # clamped
    per_read = [ref, AbsorbanceReference(0, 500, 0, 1000)]  # second read's sample channel is half as sensitive
    assert optical_density(per_read, [(500, 1000), (250, 1000)]) == pytest.approx(-math.log10(0.5))
    with pytest.raises(CalibrationError):
        optical_density([AbsorbanceReference(5, 5, 0, 1000)], [(500, 1000)])


def test_stage_coordinates_of_a_96_well_plate():
    backend = TecanInfiniteBackend(transport=FakeInfinite())
    scans = backend._plan(DEFAULT_CATALOG.get("corning_96_wellplate_360ul_flat"), ["A1", "A12", "H3", "H12"], serpentine=True)
    assert [(s.row, s.y, s.x_start, s.x_end) for s in scans] == [(0, 11230, 14370, 113370), (7, 74230, 113370, 32370)]
    assert len(scans[0].columns) == 12  # whole row span between the requested wells


# -- connection ---------------------------------------------------------------------------------------

async def test_connect_initialises_and_waits_out_the_busy_time():
    fake = FakeInfinite()
    backend = TecanInfiniteBackend(transport=fake, init_timeout_s=30, packet_timeout_s=1000)
    hub = DeviceHub([PlateReader("reader", backend, labware=CATALOG)])
    await hub.start()
    assert fake.commands == ["QQ", "INIT FORCE", "MODE ABS", "#BEAM DIAMETER"]
    assert timeouts_after(fake, "INIT FORCE")[1] == pytest.approx(80, abs=0.5)  # read after BY#T80000
    info = hub.describe_device("reader")
    assert info["state"] == "idle" and info["driver"]["absorbance_beam_diameter"] == 700
    limits = info["limits"]
    assert limits["features"] == ["absorbance", "fluorescence", "luminescence", "temperature", "shaking", "tray"]
    assert limits["max_gain"] == 255 and limits["shake_modes"] == ["orbital", "linear"] and limits["min_rpm"] is None
    assert {"test_2x3_plate", "corning_96_wellplate_360ul_flat"} <= set(limits["plates"])
    assert "generic_96_wellplate" not in limits["plates"]  # no geometry
    await hub.stop()
    assert fake.commands[-2:] == END and not fake.is_open


async def test_connect_tolerates_a_silent_status_query_and_combined_busy_frames():
    fake = FakeInfinite(busy_with_st=True)
    fake.fail["QQ"] = []
    hub, _ = await start(fake, reply_timeout_s=0.05)
    await hub.stop()


# -- reads ----------------------------------------------------------------------------------------------

ABS_SETUP = ["KEYLOCK ON", "MODE ABS", "EXCITATION CLEAR", *CLEARS, "EXCITATION 0,ABS,6000,90,0", "EXCITATION 1,ABS,6000,90,0",
             "READS 0,NUMBER=25", "READS 1,NUMBER=25", "TIME 0,READDELAY=0", "TIME 1,READDELAY=0", "SCAN DIRECTION=ALTUP",
             "#RATIO LABELS", "BEAM DIAMETER=700", "RATIO LABELS=1", "PREPARE REF"]
ABS_END = ["TERMINATE", "CHECK MTP.STEPLOSS", "CHECK ABS.STEPLOSS", *END]


def od(signal: int) -> float:
    return -math.log10(signal / 1000)


@pytest.mark.parametrize("calibration_at", ["prepare", "scan", "end"])
async def test_absorbance_run(calibration_at):
    hub, fake = await start(FakeInfinite(calibration_at=calibration_at))
    fake.signal = lambda x, y: x // 100 + y // 1000  # A1 -> 38, A3 -> 238, B1 -> 46
    job = await run(hub, "read_absorbance", {"plate": "test_2x3_plate", "wavelength_nm": 600})
    assert job.status == "succeeded", job.error
    assert fake.commands == ["?ABSOLUTE MTP,POS", *ABS_SETUP,
                             "ABSOLUTE MTP,Y=8000", "ABSOLUTE MTP,X=3000,Y=8000", "SCAN DIRECTION=ALTUP", "SCANX 3000,23000,3",
                             "ABSOLUTE MTP,Y=16000", "ABSOLUTE MTP,X=23000,Y=16000", "SCAN DIRECTION=ALTUP", "SCANX 23000,3000,3",
                             *ABS_END]
    expected = {f"{row}{col}": od((3 + 10 * (col - 1)) * 10 + y) for row, y in (("A", 8), ("B", 16)) for col in (1, 2, 3)}
    assert job.result["values"] == pytest.approx(expected)
    await hub.stop()


async def test_absorbance_subset_and_gapped_rows():
    hub, fake = await start(FakeInfinite(chunk_size=5, status_frames=True))
    job = await run(hub, "read_absorbance", {"plate": "test_2x3_plate", "wavelength_nm": 600, "wells": ["A2", "A3", "B1", "B2"]})
    assert job.status == "succeeded", job.error
    assert [c for c in fake.commands if c.startswith(("ABSOLUTE MTP,", "SCANX"))][:6] == [
        "ABSOLUTE MTP,Y=8000", "ABSOLUTE MTP,X=13000,Y=8000", "SCANX 13000,23000,2",
        "ABSOLUTE MTP,Y=16000", "ABSOLUTE MTP,X=13000,Y=16000", "SCANX 13000,3000,2"]
    assert job.result["values"] == pytest.approx({w: 0.3010299956639812 for w in ("A2", "A3", "B1", "B2")})

    fake.commands.clear()
    fake.signal = lambda x, y: x // 100
    job = await run(hub, "read_absorbance", {"plate": "test_2x3_plate", "wavelength_nm": 300, "wells": ["A1", "A3"]})
    assert "SCANX 3000,23000,3" in fake.commands and "EXCITATION 0,ABS,3000,50,0" in fake.commands  # 5 nm at <= 315 nm
    assert job.result["values"] == pytest.approx({"A1": od(30), "A3": od(230)})  # A2 measured, not returned
    await hub.stop()


async def test_fluorescence_run():
    hub, fake = await start()
    job = await run(hub, "read_fluorescence", {"plate": "test_2x3_plate", "excitation_nm": 485, "emission_nm": 520})
    assert job.status == "succeeded", job.error
    label = lambda i: [f"EXCITATION {i},FI,4850,50,0", f"EMISSION {i},FI,5200,200,0", f"TIME {i},INTEGRATION=20", f"TIME {i},LAG=0",
                       f"TIME {i},READDELAY=0", f"GAIN {i},VALUE=100", f"POSITION {i},Z=20000"]
    block = ["MODE FI.TOP", "EXCITATION CLEAR", "EMISSION CLEAR", *CLEARS, *label(0), "BEAM DIAMETER=3000", "SCAN DIRECTION=UP",
             "RATIO LABELS=1", "READS 0,NUMBER=25", *label(1), "READS 1,NUMBER=25"]
    assert fake.commands == ["?ABSOLUTE MTP,POS", "KEYLOCK ON", *block, *block, "PREPARE REF",
                             "ABSOLUTE MTP,Y=8000", "ABSOLUTE MTP,X=3000,Y=8000", "SCAN DIRECTION=UP", "SCANX 3000,23000,3",
                             "ABSOLUTE MTP,Y=16000", "ABSOLUTE MTP,X=23000,Y=16000", "SCAN DIRECTION=UP", "SCANX 23000,3000,3",
                             "TERMINATE", "CHECK MTP.STEPLOSS", "CHECK FI.TOP.STEPLOSS", "CHECK FI.STEPLOSS.Z", *END]
    assert job.result["gain"] == 100 and set(job.result["values"].values()) == {500.0}

    fake.commands.clear()
    job = await run(hub, "read_fluorescence", {"plate": "test_2x3_plate", "excitation_nm": 485, "emission_nm": 520, "gain": 60,
                                               "focal_height_mm": 7.5, "flashes": 10, "wells": ["B2"]})
    assert job.result["gain"] == 60 and job.result["values"] == {"B2": 500.0}
    assert {"GAIN 1,VALUE=60", "POSITION 0,Z=7500", "READS 1,NUMBER=10"} <= set(fake.commands)
    with pytest.raises(InvalidArgument):
        await hub.invoke("reader", "read_fluorescence", {"plate": "test_2x3_plate", "excitation_nm": 485, "emission_nm": 520,
                                                         "optics": "bottom"})
    await hub.stop()


async def test_luminescence_run():
    hub, fake = await start(FakeInfinite(calibration_at="end"))
    fake.signal = lambda x, y: 1000
    job = await run(hub, "read_luminescence", {"plate": "test_2x3_plate", "focal_height_mm": 14.62})
    assert job.status == "succeeded", job.error
    assert fake.commands == ["?ABSOLUTE MTP,POS", "KEYLOCK ON", "MODE LUM", "CHECK LUM.FIBER", "CHECK LUM.LID", "CHECK LUM.STEPLOSS",
                             "MODE LUM", "EMISSION CLEAR", *CLEARS, "POSITION LUM,Z=14620", "TIME 0,INTEGRATION=3000000",
                             "READS 0,NUMBER=25", "SCAN DIRECTION=UP", "RATIO LABELS=1", "EMISSION 1,EMPTY,0,0,0",
                             "TIME 1,INTEGRATION=1000000", "TIME 1,READDELAY=0", "READS 1,NUMBER=25", "#EMISSION ATTENUATION",
                             "PREPARE REF",
                             "ABSOLUTE MTP,Y=8000", "ABSOLUTE MTP,X=3000,Y=8000", "SCAN DIRECTION=UP", "SCANX 3000,23000,3",
                             "ABSOLUTE MTP,Y=16000", "ABSOLUTE MTP,X=3000,Y=16000", "SCAN DIRECTION=UP", "SCANX 3000,23000,3",
                             "TERMINATE", "CHECK MTP.STEPLOSS", "CHECK LUM.STEPLOSS", *END]
    assert set(job.result["values"].values()) == {1000.0}

    fake.signal = lambda x, y: x // 10  # not serpentine: B1 is still at x = 3000
    job = await run(hub, "read_luminescence", {"plate": "test_2x3_plate", "integration_s": 0.5, "wells": ["B1", "B3"]})
    assert job.result["values"] == {"B1": 600.0, "B3": 4600.0}  # counts / 0.5 s
    await hub.stop()


# -- tray, incubation, shaking ----------------------------------------------------------------------

async def test_tray():
    hub, fake = await start()
    status = await run(hub, "open_tray")
    assert fake.commands[:2] == ["ABSOLUTE MTP,OUT", "?ABSOLUTE MTP,POS"] and status["tray_open"] is True
    job = await run(hub, "read_absorbance", {"plate": "test_2x3_plate", "wavelength_nm": 600})
    assert job.status == "failed" and job.error["code"] == "needs_attention"
    fake.commands.clear()
    assert (await run(hub, "close_tray"))["tray_open"] is False and fake.commands[0] == "ABSOLUTE MTP,IN"

    fake.fail["?ABSOLUTE MTP,POS"] = ["FLOATING"]
    with pytest.raises(NeedsAttention, match="FLOATING"):
        await hub.invoke("reader", "open_tray")
    await hub.stop()


async def test_temperature():
    hub, fake = await start()
    status = await run(hub, "set_temperature", {"celsius": 37})
    assert fake.commands[:4] == ["TEMPERATURE PLATE,TARGET=370", "?TEMPERATURE PLATE,TARGET", "TEMPERATURE PLATE,STATUS=ON",
                                 "?TEMPERATURE PLATE,STATUS"]
    assert status["temperature_c"] == 21.8 and status["target_temperature_c"] == 37.0
    assert (await run(hub, "deactivate_temperature"))["target_temperature_c"] is None
    with pytest.raises(InvalidArgument, match="allowed range"):
        await hub.invoke("reader", "set_temperature", {"celsius": 45})

    fake.commands.clear()
    fake.fail["TEMPERATURE PLATE,TARGET=370"] = ["-"]
    with pytest.raises(PyreagentError, match="rejected") as err:
        await hub.invoke("reader", "set_temperature", {"celsius": 37})
    assert err.value.code == "communication_error" and fake.commands == ["TEMPERATURE PLATE,TARGET=370"]

    fake.fail["?TEMPERATURE PLATE,TARGET"] = ["369"]
    with pytest.raises(PyreagentError, match="reads back as '369'"):
        await hub.invoke("reader", "set_temperature", {"celsius": 37})
    await hub.stop()


@pytest.mark.parametrize("busy_with_st", [False, True])
async def test_shaking(busy_with_st):
    hub, fake = await start(FakeInfinite(busy_with_st=busy_with_st), packet_timeout_s=1000)
    status = await run(hub, "shake", {"duration_s": 2})
    assert status.status == "succeeded", status.error
    assert fake.commands[1:8] == ["SHAKING MODE=ORBITAL", "?SHAKING MODE", "SHAKING AMPLITUDE=1000", "?SHAKING AMPLITUDE",
                                  "SHAKING TIME=2", "?SHAKING TIME", "SHAKING ON"]
    if not busy_with_st:
        assert timeouts_after(fake, "SHAKING ON")[1] == pytest.approx(7, abs=0.1)  # duration + 5 s after BY#T2000
    fake.commands.clear()
    assert (await run(hub, "shake", {"duration_s": 1, "mode": "linear"})).status == "succeeded"
    assert "SHAKING MODE=LINEAR" in fake.commands
    with pytest.raises(InvalidArgument, match="shakes orbital, linear only"):
        await hub.invoke("reader", "shake", {"duration_s": 5, "mode": "double_orbital"})
    with pytest.raises(InvalidArgument, match="not settable"):
        await hub.invoke("reader", "shake", {"duration_s": 5, "rpm": 300})
    await hub.stop()


# -- errors -------------------------------------------------------------------------------------------

async def test_instrument_errors_become_structured():
    hub, fake = await start()
    fake.fail["CHECK LUM.LID"] = ["ERR42: lid open"]
    job = await run(hub, "read_luminescence", {"plate": "test_2x3_plate"})
    assert job.status == "failed" and job.error["code"] == "needs_attention" and "lid open" in job.error["message"]
    assert fake.commands[-5:] == ["TERMINATE", "CHECK MTP.STEPLOSS", "CHECK LUM.STEPLOSS", *END]  # the run was ended

    fake.fail["CHECK ABS.STEPLOSS"] = ["ERR7: step loss"]
    job = await run(hub, "read_absorbance", {"plate": "test_2x3_plate", "wavelength_nm": 600})
    assert job.status == "failed" and job.error["code"] == "hardware_error" and "step loss" in job.error["message"]
    await hub.stop()

    hub, fake = await start()
    fake.fail["?TEMPERATURE PLATE,CURRENT"] = ["ERR123: sensor failed"]
    with pytest.raises(PyreagentError, match="ERR123: sensor failed") as err:
        await hub.invoke("reader", "status")
    assert err.value.code == "hardware_error"
    await hub.stop()


async def test_rejections_and_protocol_violations():
    hub, fake = await start()
    fake.fail["BEAM DIAMETER=700"] = ["-"]
    job = await run(hub, "read_absorbance", {"plate": "test_2x3_plate", "wavelength_nm": 600})
    assert job.status == "failed" and job.error["code"] == "communication_error" and "rejected" in job.error["message"]
    assert fake.commands[-5:] == ABS_END

    fake.fail["?ABSOLUTE MTP,POS"] = ["IN", "ST"]  # a query must answer exactly one frame
    with pytest.raises(PyreagentError, match="expected one reply"):
        await hub.invoke("reader", "status")
    await hub.stop()

    hub, fake = await start(FakeInfinite(calibration_at=None))
    job = await run(hub, "read_absorbance", {"plate": "test_2x3_plate", "wavelength_nm": 600})
    assert job.status == "failed" and "calibration" in job.error["message"]
    await hub.stop()

    hub, fake = await start(strict_setup=False)
    fake.fail["BEAM DIAMETER=700"] = ["-"]
    assert (await run(hub, "read_absorbance", {"plate": "test_2x3_plate", "wavelength_nm": 600})).status == "succeeded"
    await hub.stop()


async def test_timeouts_and_cancel():
    hub, fake = await start(reply_timeout_s=0.05)
    fake.fail["ABSOLUTE MTP,Y=16000"] = []
    job = await run(hub, "read_absorbance", {"plate": "test_2x3_plate", "wavelength_nm": 600})
    assert job.status == "failed" and job.error["code"] == "timeout"
    assert fake.commands[-5:] == ABS_END
    assert (await run(hub, "read_absorbance", {"plate": "test_2x3_plate", "wavelength_nm": 600})).status == "succeeded"
    await hub.stop()

    hub, fake = await start()
    fake.fail["SCANX 3000,23000,3"] = []  # the reader goes quiet mid-run
    accepted = await hub.invoke("reader", "read_absorbance", {"plate": "test_2x3_plate", "wavelength_nm": 600})
    await asyncio.sleep(0.05)
    await hub.jobs.cancel(accepted.job.id)
    assert (await hub.jobs.wait(accepted.job.id, 5)).status == "cancelled"
    assert fake.commands[-5:] == ABS_END
    await hub.stop()
