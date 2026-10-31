"""BMG CLARIOstar driver: framing and RUN encoding against the spec's recorded frames, then every read end to end."""

from __future__ import annotations

import asyncio
import logging
import math

import pytest

from fake_clariostar import (
    ABS_DATA_RECORDED, ABS_RUN_RECORDED, FL_RECORDED_COUNTS, FL_RUN_RECORDED, FakeClariostar, FakeLink, known_od, known_rfu, known_rlu,
)
from pyreagent.core import DeviceHub, PyreagentError
from pyreagent.devices.plate_reader import PlateReader
from pyreagent.devices.plate_reader.bmg_labtech import DRIVER, ClariostarBackend
from pyreagent.devices.plate_reader.bmg_labtech.protocol import (
    INIT_REQUEST, MODE_ABSORBANCE, MODE_FLUORESCENCE, MODE_LUMINESCENCE, REQUEST_DATA, REQUEST_FIRMWARE, REQUEST_PARAMETERS,
    STATUS_REQUEST, STOP_REQUEST, ForbiddenCommand, FrameError, PlateGeometry, Status, absorbance_run, band, channel_block, decode,
    decode_data, drawer, encode, fluorescence_run, frame_size, luminescence_run, optical_densities, run_start, setpoint, temperature,
    well_mask,
)
from pyreagent.labware import DEFAULT_CATALOG, Labware, LabwareCatalog, LabwareGeometry

H = bytes.fromhex
PLATE96 = "corning_96_wellplate_360ul_flat"
PLATE384 = "corning_384_wellplate_112ul_flat"
COLUMN1 = [f"{row}1" for row in "ABCDEFGH"]


def labware(id: str, rows: int, columns: int, size: tuple[float, float], a1_from_back: tuple[float, float],
            last_from_back: tuple[float, float]) -> Labware:
    """A plate whose geometry reproduces one of the spec's recorded plate blocks (y given from the back edge)."""
    (sx, sy), (ax, ay), (lx, ly) = size, a1_from_back, last_from_back
    geometry = LabwareGeometry(size_x_mm=sx, size_y_mm=sy, size_z_mm=14.2, a1_x_mm=ax, a1_y_mm=sy - ay,
                               pitch_x_mm=(lx - ax) / (columns - 1), pitch_y_mm=(ly - ay) / (rows - 1),
                               well_bottom_z_mm=1, well_depth_mm=10, well_diameter_mm=6.58)
    return Labware(id=id, rows=rows, columns=columns, geometry=geometry)


SPEC96 = labware("spec_96", 8, 12, (127.76, 85.48), (14.30, 11.28), (113.30, 74.28))
SPEC384 = labware("spec_384", 16, 24, (127.80, 85.50), (12.12, 8.99), (115.68, 76.51))
CATALOG = LabwareCatalog([*DEFAULT_CATALOG.list(), SPEC96, SPEC384])


def make(fake: FakeClariostar | None = None, **options) -> tuple[ClariostarBackend, FakeLink, FakeClariostar]:
    fake = fake or FakeClariostar()
    link = FakeLink(fake)
    options = {"poll_interval_s": 0, "silence_s": 0.01, "reply_timeout_s": 0.01, **options}
    return ClariostarBackend(transport=link, **options), link, fake


async def start(fake: FakeClariostar | None = None, **options) -> tuple[DeviceHub, FakeLink, FakeClariostar]:
    backend, link, fake = make(fake, **options)
    hub = DeviceHub([PlateReader("clario", backend, labware=CATALOG, poll_interval_s=0)])
    await hub.start()
    return hub, link, fake


async def run(hub: DeviceHub, capability: str, args: dict) -> dict:
    accepted = await hub.invoke("clario", capability, args)
    if accepted.job is None:
        return accepted.result
    job = await hub.jobs.wait(accepted.job.id, 5)
    assert job.status == "succeeded", job.error
    return job.result


async def outcome(hub: DeviceHub, capability: str, args: dict) -> dict | None:
    try:
        accepted = await hub.invoke("clario", capability, args)
    except PyreagentError as err:
        return err.to_dict()
    return accepted.job and (await hub.jobs.wait(accepted.job.id, 5)).error


# -- framing -------------------------------------------------------------------------------------------------

def test_request_frames_match_the_recorded_bytes():
    assert encode(STATUS_REQUEST) == H("020009 0c80 000097 0d")
    assert encode(INIT_REQUEST) == H("02000d0c0100001003 00002f 0d")
    assert encode(REQUEST_PARAMETERS) == H("02000f0c050700000000000000290d")
    assert encode(REQUEST_FIRMWARE) == H("02000f0c0509000000000000002b0d")
    assert encode(REQUEST_DATA) == H("02000f0c050200000000000000240d")
    assert encode(drawer(True)) == H("02000e0c0301000000000000200d")
    assert encode(drawer(False)) == H("02000e0c03000000000000001f0d")
    assert encode(temperature(1)) == H("02000b0c0600010000200d")  # monitor
    assert encode(temperature(0)) == H("02000b0c06000000001f0d")  # off
    assert encode(temperature(setpoint(20.0))) == H("02000b0c0600c80000e70d")
    assert encode(STOP_REQUEST) == H("02000a0c0b000000230d")


def test_reply_frames_decode_by_length_and_checksum():
    init = FakeClariostar()(encode(INIT_REQUEST))
    assert len(init) == 476 and frame_size(init[:3]) == 476 and b"\r" in init[4:-4]  # CR inside the payload
    assert decode(init)[0] == 0x13
    data = H("02004a0c022506260000a1000800080003f7a0010001000801000100000001010000000000f0000000440000003e0000004800000040000000430000"
             "0045000000430000004400060d0d")
    assert decode(data)[-1] == 0x44 and data[-2] == 0x0D  # 0x0D inside the checksum, then the real CR
    page = decode_data(decode(data))
    assert (page.count, page.wells, page.temperature_c, page.ceiling) == (8, 8, 24.0, 260_000)
    assert list(page.values) == FL_RECORDED_COUNTS


@pytest.mark.parametrize("frame, problem", [
    (H("0200180c010500210000000000000000000d"), "size"),  # the spec's truncated status (18 of 24 bytes)
    (H("0200180c010506240000000000000000000000c00001170d"), "checksum"),
    (H("0200180c010506240000000000000000000000c00001160a"), "end byte"),
    (H("0200180d010506240000000000000000000000c00001170d"), "reader id"),
])
def test_corrupt_frames_are_rejected(frame, problem):
    with pytest.raises(FrameError, match=problem):
        decode(frame)


def test_status_bits_from_recorded_replies():
    def status(hexframe: str) -> Status:
        return Status.decode(decode(H(hexframe)))

    idle = status("0200180c010506240000000000000000000000c00001160d")
    assert not idle.busy and not idle.data_ready and not idle.drawer_open and not idle.plate_detected and idle.motors_enabled
    moving = status("0200180c012506240000030000000000000000c00001390d")
    assert moving.busy and moving.running == 0x03
    assert status("0200180c010506210000000000000000000000c00001130d").drawer_open
    assert status("0200180c010500260000000000000000000000c00001120d").plate_detected
    warm = status("0200180c010500240000000000000000e700f0c00002e70d")
    assert (warm.bottom_c, warm.top_c, warm.temperature_c) == (23.1, 24.0, 23.55)
    assert not status("0200180c010500040000000000000000000000c00000f00d").motors_enabled
    refused = status("0200180c011500000000c90000000000000000c00001c50d")
    assert refused.refused and refused.quit_code == 201
    collision = status("0200180c01a504260000fa0500000000000000c00002b50d")
    assert collision.warning and not collision.refused and collision.codes[:2] == (250, 5)
    ready = status("0200180c010507260000000000000000000000c00001190d")
    assert ready.data_ready and not ready.busy


def test_set_parameter_is_never_encoded():
    with pytest.raises(ForbiddenCommand):
        encode(H("0e0b12000000ff"))  # the one recorded EEPROM write


# -- RUN encoding ----------------------------------------------------------------------------------------------

def test_run_plate_block_for_96_and_384_plates():
    backend, _, _ = make()
    geometry, cells, ordered = backend._plate(SPEC96, ["B1", "A1", "C2", "D2"])
    assert geometry.encode() == H("31e8 2164 0596 0468 2c42 1d04 0c 08")  # the spec's standard 96 plate
    assert ordered == ["A1", "B1", "C2", "D2"]
    assert well_mask(12, cells)[:6] == H("800800400400")
    assert well_mask(12, [(r, 0) for r in range(8)]) == H("800800" * 4) + bytes(36)
    assert well_mask(12, [(r, c) for r in range(8) for c in range(12)]) == b"\xff" * 12 + bytes(36)

    geometry, cells, _ = backend._plate(SPEC384, ["A1", "A24", "B1", "P24"])
    assert geometry.encode() == H("31ec 2166 04bc 0383 2d30 1de3 18 10")  # the spec's 384 example
    mask = well_mask(24, cells)
    assert (mask[0], mask[2], mask[3], mask[47]) == (0x80, 0x01, 0x80, 0x01)  # bits 0, 23, 24, 383

    # Labware y grows toward the back: an Opentrons-derived plate gets its back-edge distances converted.
    corning = CATALOG.get(PLATE96)
    geometry, _, _ = backend._plate(corning, ["A1"])
    assert geometry.a1_y_mm == pytest.approx(85.47 - 74.24) and geometry.last_y_mm == pytest.approx(85.47 - 74.24 + 63)


def test_run_frames_match_the_recorded_runs():
    col1 = [(r, 0) for r in range(8)]
    g96 = PlateGeometry(127.76, 85.48, 14.30, 11.28, 113.30, 74.28, 12, 8)
    absorbance = run_start(g96, col1, mode=MODE_ABSORBANCE, settling_s=0.1)
    assert encode(absorbance_run(absorbance, [600], flashes=5)) == ABS_RUN_RECORDED
    assert encode(absorbance_run(absorbance, [260, 280, 350, 450, 530, 600, 700, 750], flashes=5)) == H(
        "02009e0c0431e82164059604682c421d040c08008008008008008008008008000000000000000000000000000000000000000000000000000000000000"
        "000000000000000a02000000000000000000000000000000000000000000000000000000000000270f270f05080a280af00dac119414b417701b581d4c"
        "00000064232826ca000000640000000502000000000001000000010005000100000d640d")
    assert encode(absorbance_run(absorbance, [], flashes=5, spectrum=(300, 700, 1))) == H(
        "0200900c0431e82164059604682c421d040c08008008008008008008008008000000000000000000000000000000000000000000000000000000000000"
        "000000000000000a02000000000000000000000000000000000000000000000000000000000000270f270f05000bb81b58000a232826ca00000064000000"
        "05020000000000010000000100050001000009750d")
    fluorescence = run_start(g96, col1, mode=MODE_FLUORESCENCE, settling_s=0.1)
    assert band(485, 15) == (0x133D, 0x12A7)
    assert encode(fluorescence_run(fluorescence, [channel_block(1000, band(485, 15), band(528, 20))], focal_height_mm=8.5,
                                   flashes=5)) == FL_RUN_RECORDED
    # Bottom optic, all wells, vendor band edges, 10 flashes (vector 14).
    g = PlateGeometry(127.80, 85.50, 14.30, 11.20, 113.50, 74.30, 12, 8)
    every = [(r, c) for r in range(8) for c in range(12)]
    bottom = run_start(g, every, mode=MODE_FLUORESCENCE, bottom=True, settling_s=0.1)
    assert encode(fluorescence_run(bottom, [channel_block(1000, (0x133B, 0x12A9), (0x1501, 0x143F), 0x13BC)], focal_height_mm=8.5,
                                   flashes=10)) == H(
        "02009c0c0431ec2166059604602c561d060c0800ffffffffffffffffffffffff00000000000000000000000000000000000000000000000000000000"
        "00000000000000000a40000000000000000000000000000000000000000000000000000000000000270f270f05035200000100000000000c03e8133b"
        "12a913bc1501143f00040003000000000000000000000100000001000a00010000145b0d")
    # Luminescence, all wells, 580/80 with the vendor's edges, gain 3500, 1.00 s normalization, 0.80 s interval (vector 15).
    g = PlateGeometry(127.76, 85.48, 14.30, 11.20, 113.46, 74.28, 12, 8)
    luminescence = run_start(g, every, mode=MODE_LUMINESCENCE, edr=True)
    assert encode(luminescence_run(luminescence, focal_height_mm=8.5, gain=3500, emission=(0x1834, 0x151C), normalization_s=1.0,
                                   interval_s=0.8)) == H(
        "02009a0c0431e82164059604602c521d040c0800ffffffffffffffffffffffff00000000000000000000000000000000000000000000000000000000"
        "00000000000000000a01400000000000000000000000000000000000000000000000000000000000270f270f01035200000100000dac000100010001"
        "1834151c00050000006400000000000000000000010000000100500001000012f30d")


def test_od_formula_against_the_recorded_absorbance_reply():
    vendor = decode_data(decode(H(
        "0200cc0c020506260000a900280028001dffe202200300010008010001000000010100000000000000321a740021b78100176887000b112a0002c8c50000"
        "b52400003b5f000017fe0013acbe00132d3c0013781700138a3100136693001377d8001393d10013381600089c6d00084714000879bf00086a420008"
        "6b7000086fa300086f4400085f74000047be000047bd000047ae00004794000047780000478e000047a800004798003bf59500008d14001774c50000"
        "8f83000b3dc500008daa000047900000000000297a0d")))
    assert (vendor.count, vendor.wells, vendor.temperature_c) == (40, 8, None)
    od = optical_densities(vendor.values, 1, 8)
    expected = -math.log10((0x00321A74 / 0x003BF595) * (0x00004790 / 0x000047BE))
    assert od[0][0] == pytest.approx(expected) and od[0][0] == pytest.approx(0.0791, abs=1e-4)

    session = decode_data(decode(ABS_DATA_RECORDED))
    assert session.temperature_c == 24.0
    assert optical_densities(session.values, 1, 8)[0][0] == pytest.approx(0.042, abs=0.001)  # an empty well
    with pytest.raises(FrameError):
        optical_densities(session.values[:-1], 1, 8)


# -- the driver end to end ----------------------------------------------------------------------------------------

def test_driver_export_and_options():
    assert DRIVER is ClariostarBackend
    with pytest.raises(ValueError, match="port"):
        ClariostarBackend()
    assert ClariostarBackend("COM9").transport.describe() == {"transport": "serial", "port": "COM9", "baudrate": 125000}


async def test_connect_initializes_without_polling_first_and_discovers_the_reader():
    hub, link, fake = await start()
    assert link.sent[0] == encode(INIT_REQUEST)  # INIT before any status poll, sent once
    assert link.sent.count(encode(INIT_REQUEST)) == 1
    assert encode(REQUEST_PARAMETERS) in link.sent and encode(REQUEST_FIRMWARE) in link.sent
    assert link.sent[-1] == encode(temperature(1))  # temperature sensors on
    info = hub.describe_device("clario")
    assert info["state"] != "offline" and info["driver"]["model"] == "CLARIOstar Plus"
    assert info["driver"]["firmware"] == "1.35" and info["driver"]["firmware_built"] == "Nov 20 2020 11:51:21"
    limits = info["limits"]
    assert limits["features"] == ["absorbance", "absorbance_spectrum", "fluorescence", "luminescence", "temperature", "tray"]
    assert (limits["min_wavelength_nm"], limits["max_wavelength_nm"], limits["max_spectrum_points"]) == (220, 1000, 781)
    assert (limits["min_excitation_nm"], limits["max_emission_nm"], limits["min_stokes_shift_nm"]) == (320, 840, 18)
    assert limits["optics"] == ["top", "bottom"] and (limits["min_gain"], limits["max_gain"]) == (0, 4095)
    assert (limits["min_temperature_c"], limits["max_temperature_c"]) == (25.0, 45.0)
    assert PLATE96 in limits["plates"] and "generic_96_wellplate" not in limits["plates"]  # geometry is required
    names = {c["name"] for c in info["capabilities"]}
    assert "shake" not in names and {"open_tray", "read_luminescence"} <= names
    await hub.stop()
    assert link.sent[-1] == encode(temperature(0)) and not link.is_open


async def test_motors_off_after_init_fails_the_connection():
    fake = FakeClariostar()
    fake.motors_off = True
    backend, link, _ = make(fake)
    with pytest.raises(PyreagentError, match="motors"):
        await backend.connect()
    assert not link.is_open


async def test_absorbance_read_reproduces_the_recorded_session():
    hub, link, fake = await start()
    result = await run(hub, "read_absorbance", {"plate": "spec_96", "wavelength_nm": 600, "wells": COLUMN1})
    assert ABS_RUN_RECORDED in link.sent
    assert result["values"]["A1"] == pytest.approx(0.042, abs=0.001) and list(result["values"]) == COLUMN1
    await hub.stop()


async def test_absorbance_read_of_selected_wells():
    hub, link, fake = await start()
    wells = ["H12", "A1", "C6"]
    result = await run(hub, "read_absorbance", {"plate": PLATE96, "wavelength_nm": 450, "wells": wells})
    assert list(result["values"]) == wells
    for well, index in (("A1", 0), ("C6", 29), ("H12", 95)):
        assert result["values"][well] == pytest.approx(known_od(index, 450), abs=2e-4)
    assert fake.runs[-1][101:104] == H("01 1194")  # one wavelength, 450.0 nm
    await hub.stop()


async def test_absorbance_spectrum_collects_every_page():
    hub, link, fake = await start()
    fake.page_values = 50
    wells = ["A1", "B2", "P24"]
    result = await run(hub, "read_absorbance_spectrum", {"plate": PLATE384, "start_nm": 400, "end_nm": 440, "step_nm": 10,
                                                         "wells": wells})
    assert result["wavelengths_nm"] == [400, 410, 420, 430, 440]
    assert link.sent.count(encode(REQUEST_DATA)) == math.ceil((5 + 3) * (3 + 2) / 50)  # 40 values in 50s: one page
    fake.page_values = 7
    result = await run(hub, "read_absorbance_spectrum", {"plate": PLATE384, "start_nm": 400, "end_nm": 440, "step_nm": 10,
                                                         "wells": wells})
    assert link.sent.count(encode(REQUEST_DATA)) == 1 + math.ceil(40 / 7)
    for well, index in (("A1", 0), ("B2", 25), ("P24", 383)):
        assert result["values"][well] == pytest.approx([known_od(index, nm) for nm in range(400, 441, 10)], abs=2e-4)
    assert fake.runs[-1][101:108] == H("00 0fa0 1130 0064")  # spectrum: 400.0 to 440.0 nm, step 10.0
    await hub.stop()


async def test_fluorescence_reproduces_the_recorded_read():
    hub, link, fake = await start()
    result = await run(hub, "read_fluorescence", {"plate": "spec_96", "excitation_nm": 485, "emission_nm": 528, "wells": COLUMN1,
                                                  "gain": 1000, "focal_height_mm": 8.5, "flashes": 5})
    assert FL_RUN_RECORDED in link.sent
    assert [result["values"][w] for w in COLUMN1] == FL_RECORDED_COUNTS and result["gain"] == 1000
    await hub.stop()


async def test_fluorescence_bottom_optic_with_data_ready_while_busy():
    hub, link, fake = await start()
    fake.data_while_busy = True
    result = await run(hub, "read_fluorescence", {"plate": PLATE96, "excitation_nm": 485, "emission_nm": 535, "wells": ["B3"],
                                                  "optics": "bottom"})
    assert result["values"] == {"B3": known_rfu(14)} and result["gain"] == 1000  # the default gain is reported
    run_payload = fake.runs[-1]
    assert run_payload[65] == 0x40 and run_payload[101:103] == H("0352")  # bottom optic, 8.5 mm default focal height
    assert run_payload[112:114] == H("03e8")  # gain 1000
    error = await outcome(hub, "read_fluorescence", {"plate": PLATE96, "excitation_nm": 485, "emission_nm": 495})
    assert error["code"] == "invalid_argument"  # the bands would overlap
    await hub.stop()


async def test_luminescence_read():
    hub, link, fake = await start()
    result = await run(hub, "read_luminescence", {"plate": PLATE96, "integration_s": 0.5, "wells": ["A1", "H12"]})
    assert result["values"] == {"A1": known_rlu(0), "H12": known_rlu(95)}
    payload = fake.runs[-1]
    assert payload[65:67] == H("0140") and payload[100] == 0x01  # luminescence, EDR, no settling
    assert payload[-5:-3] == H("0032")  # 0.50 s interval in the flashes slot
    assert payload[-22:-20] == H("0064")  # normalized to 1.00 s
    await hub.stop()


async def test_drawer_and_plate_checks():
    hub, link, fake = await start()
    assert (await run(hub, "open_tray", {}))["tray_open"] is True
    assert encode(drawer(True)) in link.sent
    error = await outcome(hub, "read_absorbance", {"plate": PLATE96, "wavelength_nm": 600})
    assert error["code"] == "needs_attention" and "tray is open" in error["message"]
    assert (await run(hub, "close_tray", {}))["tray_open"] is False
    fake.plate = False
    error = await outcome(hub, "read_absorbance", {"plate": PLATE96, "wavelength_nm": 600})
    assert error["code"] == "needs_attention" and "plate" in error["message"]
    error = await outcome(hub, "read_absorbance", {"plate": "nest_96_wellplate_2ml_deep", "wavelength_nm": 600})
    assert error["code"] == "invalid_argument" and "41 mm tall" in error["message"]
    assert not fake.runs
    await hub.stop()


async def test_temperature_control():
    hub, link, fake = await start()
    status = await run(hub, "set_temperature", {"celsius": 37})
    assert encode(temperature(370)) in link.sent and status["target_temperature_c"] == 37
    status = await run(hub, "wait_for_temperature", {"tolerance_c": 0.5})
    assert abs(status["temperature_c"] - 37) <= 0.5
    status = await run(hub, "deactivate_temperature", {})
    assert [f for f in link.sent if f[4] == 0x06][-1] == encode(temperature(1)) and status["target_temperature_c"] is None
    error = await outcome(hub, "set_temperature", {"celsius": 20})
    assert error["code"] == "invalid_argument"  # below the standard incubator's range, refused before sending
    await hub.stop()


async def test_extended_incubator_range_and_the_readers_refusal():
    hub, link, fake = await start(extended_temperature=True)
    limits = hub.describe_device("clario")["limits"]
    assert (limits["min_temperature_c"], limits["max_temperature_c"]) == (10.0, 65.0)
    error = await outcome(hub, "set_temperature", {"celsius": 20})  # this fake has a standard incubator: quit code 110
    assert error["code"] == "invalid_argument" and error["details"]["quit_code"] == 110
    await hub.stop()


@pytest.mark.parametrize("code, error_code", [(243, "needs_attention"), (242, "hardware_error"), (65, "invalid_argument"),
                                              (202, "device_busy")])
async def test_refused_run_maps_quit_codes(code, error_code):
    hub, link, fake = await start()
    fake.refuse[0x04] = code
    error = await outcome(hub, "read_absorbance", {"plate": PLATE96, "wavelength_nm": 600})
    assert error["code"] == error_code and error["details"]["quit_code"] == code
    assert fake.stopped == 0 and encode(STOP_REQUEST) not in link.sent  # nothing was started
    await hub.stop()


async def test_collision_warning_is_logged_not_raised(caplog):
    hub, link, fake = await start()
    fake.warning = (250, 5)
    with caplog.at_level(logging.WARNING, "pyreagent.clariostar"):
        assert (await run(hub, "status", {}))["tray_open"] is False
        await run(hub, "status", {})
    assert sum("collision" in r.message for r in caplog.records) == 1  # once per change of codes
    await hub.stop()


async def test_bad_checksum_is_retried_then_reported():
    hub, link, fake = await start()
    fake.corrupt = 2  # two corrupt status replies: STATUS gets 5 attempts
    assert (await run(hub, "status", {}))["tray_open"] is False

    backend, link, fake = make()
    await backend.connect()
    fake.corrupt = 5
    with pytest.raises(PyreagentError) as raised:
        await backend.tray_is_open()
    assert raised.value.code == "communication_error" and "checksum" in raised.value.message
    await backend.disconnect()


async def test_corrupt_data_page_stops_the_run():
    hub, link, fake = await start()
    real_request = fake._request

    def corrupt_data(command: int) -> bytes:
        if command == 0x02:
            fake.corrupt = 1
        return real_request(command)

    fake._request = corrupt_data  # type: ignore[method-assign]
    error = await outcome(hub, "read_absorbance", {"plate": PLATE96, "wavelength_nm": 600, "wells": ["A1"]})
    assert error["code"] == "communication_error"
    assert link.sent.count(encode(REQUEST_DATA)) == 1  # a data page is never re-requested
    assert encode(STOP_REQUEST) in link.sent
    await hub.stop()


async def test_cancelling_a_read_stops_the_reader():
    hub, link, fake = await start()
    fake.hang = True
    accepted = await hub.invoke("clario", "read_absorbance", {"plate": PLATE96, "wavelength_nm": 600})
    await asyncio.sleep(0.05)
    await hub.jobs.cancel(accepted.job.id)
    assert (await hub.jobs.wait(accepted.job.id, 5)).status == "cancelled"
    assert fake.stopped == 1 and encode(STOP_REQUEST) in link.sent and not link.rx
    assert (await run(hub, "status", {}))["tray_open"] is False
    await hub.stop()


async def test_cancelling_mid_reply_drains_the_line():
    hub, link, fake = await start()
    fake.page_values = 1000  # one data page of 1612 bytes...
    link.stall_at = 1000  # ...that stops arriving part-way
    accepted = await hub.invoke("clario", "read_absorbance", {"plate": PLATE96, "wavelength_nm": 600})
    for _ in range(100):
        if link.stall_at is None:
            break
        await asyncio.sleep(0.01)
    assert link.stall_at is None and link.rx  # stalled with the rest of the page unread
    await hub.jobs.cancel(accepted.job.id)
    assert (await hub.jobs.wait(accepted.job.id, 5)).status == "cancelled"
    assert link.sent[-1] == encode(STOP_REQUEST) and not link.rx  # the half-read page was drained, not taken as STOP's reply
    assert (await run(hub, "status", {}))["tray_open"] is False
    await hub.stop()


async def test_measurement_timeout_stops_the_run():
    hub, link, fake = await start(read_timeout_s=0.05)
    fake.hang = True
    error = await outcome(hub, "read_luminescence", {"plate": PLATE96, "integration_s": 0.1, "wells": ["A1"]})
    assert error["code"] == "timeout" and fake.stopped == 1
    await hub.stop()
