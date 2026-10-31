"""SpectraMax M5 / 384 Plus / Gemini EM driver against a protocol fake, with the vectors from the protocol notes."""

from __future__ import annotations

import asyncio
import math

import pytest

from fake_spectramax import FakeSpectraMax, default_value
from pyreagent.core import (
    CommunicationError,
    DeviceBusy,
    DeviceHub,
    DeviceTimeout,
    HardwareError,
    InvalidArgument,
    NeedsAttention,
    NotSupported,
    PyreagentError,
)
from pyreagent.devices.plate_reader import PlateReader
from pyreagent.devices.plate_reader.molecular_devices import SpectraMaxBackend
from pyreagent.devices.plate_reader.molecular_devices.protocol import (
    check_reply,
    cutoff_filter,
    encode,
    error_for,
    expected_fields,
    filter_commands,
    mode,
    number,
    parse_data,
    pmt_commands,
    set_temperature,
    settling_time,
    shake_commands,
    split_fields,
    strip,
    xpos,
    ypos,
)
from pyreagent.io import MockTransport
from pyreagent.labware import DEFAULT_CATALOG, Labware, LabwareCatalog, LabwareGeometry

# The protocol notes' test plate: A1 centre 13.38 mm from the left edge and 12.24 mm from the back edge.
PLATE = Labware(id="test_96_plate", rows=8, columns=12, geometry=LabwareGeometry(
    size_x_mm=127.76, size_y_mm=85.48, size_z_mm=14.2, a1_x_mm=13.38, a1_y_mm=85.48 - 12.24, well_bottom_z_mm=1.0, well_depth_mm=10.7))
CATALOG = LabwareCatalog([PLATE, DEFAULT_CATALOG.get("generic_96_wellplate")])
CONNECT = ["!", "!OPTION"]


async def start(fake: FakeSpectraMax, **options) -> DeviceHub:
    backend = SpectraMaxBackend(model=fake.model, transport=MockTransport(fake), poll_interval_s=0, **options)  # type: ignore[arg-type]
    hub = DeviceHub([PlateReader("reader", backend, labware=CATALOG, poll_interval_s=0.01)])
    await hub.start()
    return hub


async def run(hub: DeviceHub, capability: str, args: dict | None = None) -> dict:
    accepted = await hub.invoke("reader", capability, args or {})
    if accepted.job is None:
        return accepted.result
    job = await hub.jobs.wait(accepted.job.id, 5)
    assert job.status == "succeeded", job.error
    return job.result


async def failure(hub: DeviceHub, capability: str, args: dict) -> dict:
    try:
        accepted = await hub.invoke("reader", capability, args)
    except PyreagentError as err:
        return err.to_dict()
    job = await hub.jobs.wait(accepted.job.id, 5)
    assert job.status == "failed"
    return job.error


def after_connect(fake: FakeSpectraMax) -> list[str]:
    assert fake.commands[:2] == CONNECT
    return fake.commands[2:]


# -- protocol vectors -------------------------------------------------------------------------------

def test_field_count_table():
    cases = [("!", "m5", 1), ("!STATUS", "m5", 2), ("!TRANSFER", "gemini_em", 2), ("!SPEED OFF", "m5", 2),
             ("!READTYPE ABSPLA", "m5", 2), ("!READTYPE PLA", "384_plus", 1), ("!READTYPE FLU", "m5", 1),
             ("!READTYPE FLUCUV", "m5", 2), ("!READTYPE LUM", "gemini_em", 1), ("!TEMP", "gemini_em", 2),
             ("!TEMP 37.0", "m5", 2), ("!TEMP 26.0", "gemini_em", 1), ("!READSTAGE TOP", "m5", 2),
             ("!READSTAGE BOT", "gemini_em", 1), ("!WELLSCANMODE ON", "gemini_em", 1), ("!OPTION", "m5", 2),
             ("!TOPREADCLEAR ON", "gemini_em", 1), ("!CLEAR DATA", "m5", 1)]
    assert [expected_fields(command, model) for command, model, _ in cases] == [count for *_, count in cases]


def test_command_text_matches_the_vectors():
    assert encode("!") == b"!\r"
    assert (xpos(13.38, 9, 12), ypos(12.24, 9, 8), strip(1, 12)) == ("!XPOS 13.380 9.000 12", "!YPOS 12.240 9.000 8", "!STRIP 1 12")
    assert (mode("KINETIC", 10, 5), mode("SPECTRUM", 200, 10, 50), mode("EXSPECTRUM", 200, 10, 50), mode("ENDPOINT")) == \
        ("!MODE KINETIC 10 5", "!MODE SPECTRUM 200 10 50", "!MODE EXSPECTRUM 200 10 50", "!MODE ENDPOINT")
    assert shake_commands() == ["!SHAKE OFF"]
    assert shake_commands(before_s=5) == ["!SHAKE ON", "!SHAKE 5 0 0 0 0"]
    assert shake_commands(interval_s=10, between_s=3) == ["!SHAKE ON", "!SHAKE 0 10 7 3 0"]
    assert shake_commands(5, 30, 3) == ["!SHAKE ON", "!SHAKE 5 30 27 3 0"]
    assert (pmt_commands("auto"), pmt_commands("high"), pmt_commands(9)) == \
        (["!AUTOPMT ON"], ["!AUTOPMT OFF", "!PMT HIGH"], ["!AUTOPMT OFF", "!PMT 9"])
    assert filter_commands([cutoff_filter(535), 9]) == ["!AUTOFILTER OFF", "!EMFILTER 8 9"]  # type: ignore[list-item]
    assert [cutoff_filter(nm) for nm in (520, 525, 535, 300, 323, 417, 900)] == [7, 7, 8, 1, None, None, None]
    assert (settling_time(10), settling_time(110)) == ("!NVRAM CARCOL 100", "!NVRAM CARCOL 110")
    assert (set_temperature(37), set_temperature(26.0), set_temperature(0)) == ("!TEMP 37.0", "!TEMP 26.0", "!TEMP 0")
    assert (number(300 * 0.001), number(100 * 0.001), number(5.0), number(-0.0)) == ("0.3", "0.1", "5", "0")


def test_reply_vectors():
    assert split_fields("OK>\r\n") == ["OK"] and check_reply("!", ["OK"]) is None
    assert check_reply("!TEST", split_fields("OK\tWarning: Something happened>\r\n")) == "OK\tWarning: Something happened"
    fields = split_fields("OK\t\r\n>FAIL\t 107>\r\n")
    assert fields == ["OK", "FAIL\t 107"]
    with pytest.raises(CommunicationError, match="107 \\(no data to transfer\\)") as caught:
        check_reply("!TRANSFER", fields)
    assert caught.value.details["error_code"] == 107
    with pytest.raises(HardwareError, match="unknown error code: 999"):
        check_reply("!X", split_fields("FAIL\t 999>\r\n"))
    with pytest.raises(CommunicationError, match="unparsable error"):
        check_reply("!X", split_fields("FAIL\t ABC>\r\n"))
    with pytest.raises(CommunicationError, match="neither OK nor FAIL"):
        check_reply("!X", ["HELLO"])


def test_error_codes_map_to_pyreagent_errors():
    kinds = {101: InvalidArgument, 111: InvalidArgument, 105: DeviceBusy, 106: DeviceBusy, 100: CommunicationError,
             203: CommunicationError, 110: NeedsAttention, 304: NeedsAttention, 300: HardwareError, 400: HardwareError,
             505: HardwareError, 999: HardwareError}
    assert {code: type(error_for(code, "!READ")) for code in kinds} == kinds
    assert error_for(400, "!READ").faults_device and not error_for(110, "!READ").faults_device


def test_data_vectors():
    head = "12345.6\t25.1\t96-well\r\n"
    (one,) = parse_data(head + "L:\t260\r\nL:\t260\r\n1:\t0.1\t0.2\r\n2:\t0.3\t0.4")
    assert (one.wavelengths, one.time_s, one.temperature_c, one.matrix()) == ((260,), 12345.6, 25.1, [[0.1, 0.3], [0.2, 0.4]])
    two = parse_data(head + "L:\t260\t280\nL:\t260\n1:\t0.1\t0.2\n2:\t0.3\t0.4\nL:\t280\n1:\t0.5\t0.6\n2:\t0.7\t0.8")
    assert [(b.wavelengths, b.matrix()) for b in two] == [((260,), [[0.1, 0.3], [0.2, 0.4]]), ((280,), [[0.5, 0.7], [0.6, 0.8]])]
    (fluo,) = parse_data(head + "exL:\t485\nemL:\t520\nL:\t485\t520\n1:\t100\t200\n2:\t300\t400")
    assert (fluo.wavelengths, fluo.matrix()) == ((485, 520), [[100, 300], [200, 400]])
    (lum,) = parse_data(head + "emL:\t590\nL:\t\t590\n1:\t1000\t2000\n2:\t3000\t4000")
    assert (lum.wavelengths, lum.matrix()) == ((None, 590), [[1000, 3000], [2000, 4000]])
    (odd,) = parse_data(head + "L:\t260\n1:\t0.1\t#SAT\n2:\t0.3\t-")
    assert odd.matrix() == [[0.1, 0.3], [math.inf, None]]
    (signed,) = parse_data(head + "L:\t260\n1:\t-0.012\t1.5E+04\n2:\t+2e-3\t.5")
    assert signed.matrix() == [[-0.012, 0.002], [15000.0, 0.5]]
    (first_line_is_data,) = parse_data(head + "L:\t260\n1:\t0.1")  # no summary line: nothing is skipped
    assert first_line_is_data.matrix() == [[0.1]]


# -- connection and reads ---------------------------------------------------------------------------

async def test_connect_handshake():
    fake = FakeSpectraMax()
    hub = await start(fake)
    backend = hub.device("reader").backend
    assert backend.transport.sent[:2] == [b"!\r", b"!OPTION\r"] and fake.commands == CONNECT
    driver = hub.describe_device("reader")["driver"]
    assert driver["model"] == "SpectraMax M5" and driver["firmware"] == "SPECTRAMAX M5 V2.04"
    await hub.stop()


async def test_silence_is_a_timeout():
    backend = SpectraMaxBackend(transport=MockTransport(lambda data: None), command_timeout_s=0.01)
    with pytest.raises(DeviceTimeout, match="No reply to !"):
        await backend.connect()
    assert not backend.transport.is_open  # type: ignore[attr-defined]


async def test_m5_absorbance_sequence_and_values():
    fake = FakeSpectraMax()
    hub = await start(fake)
    result = await run(hub, "read_absorbance", {"plate": "test_96_plate", "wavelength_nm": 500})
    assert after_connect(fake) == [
        "!CLEAR DATA", "!XPOS 13.380 9.000 12", "!YPOS 12.240 9.000 8", "!STRIP 1 12", "!CSPEED 8", "!SHAKE OFF",
        "!WAVELENGTH 500", "!CALIBRATE ONCE", "!MODE ENDPOINT", "!ORDER COLUMN", "!SPEED OFF", "!TAG OFF",
        "!NVRAM CARCOL 100", "!READTYPE ABSPLA", "!READ", "!STATUS", "!STATUS", "!STATUS", "!TRANSFER"]
    assert len(result["values"]) == 96
    assert result["values"]["C5"] == float(default_value("abs", 500, 2, 5))
    await hub.stop()


async def test_384_plus_absorbance_uses_its_own_read_type():
    fake = FakeSpectraMax(model="384_plus")
    hub = await start(fake, settling_time_ms=500)
    result = await run(hub, "read_absorbance", {"plate": "test_96_plate", "wavelength_nm": 405, "wells": ["H12", "A1"]})
    sent = after_connect(fake)
    assert "!READTYPE PLA" in sent and not any(c.startswith(("!TAG", "!NVRAM")) for c in sent)
    assert result["values"] == {"H12": float(default_value("abs", 405, 7, 12)), "A1": float(default_value("abs", 405, 0, 1))}
    await hub.stop()


async def test_absorbance_spectrum():
    fake = FakeSpectraMax()
    hub = await start(fake)
    result = await run(hub, "read_absorbance_spectrum", {"plate": "test_96_plate", "start_nm": 400, "end_nm": 440, "step_nm": 20,
                                                          "wells": ["B3"]})
    sent = after_connect(fake)
    assert "!MODE SPECTRUM 400 20 3" in sent and sent.count("!TRANSFER") == 3
    assert result["wavelengths_nm"] == [400, 420, 440]
    assert result["values"] == {"B3": [float(default_value("abs", nm, 1, 3)) for nm in (400, 420, 440)]}
    await hub.stop()


async def test_m5_fluorescence_and_luminescence():
    fake = FakeSpectraMax()
    hub = await start(fake)
    fluo = await run(hub, "read_fluorescence", {"plate": "test_96_plate", "excitation_nm": 485, "emission_nm": 520,
                                                "optics": "bottom", "wells": ["D4"]})
    assert after_connect(fake) == [
        "!CLEAR DATA", "!XPOS 13.380 9.000 12", "!YPOS 12.240 9.000 8", "!STRIP 1 12", "!CSPEED 8", "!SHAKE OFF", "!FPW 10",
        "!AUTOPMT ON", "!EXWAVELENGTH 485", "!EMWAVELENGTH 520", "!AUTOFILTER ON", "!READSTAGE BOT", "!PMTCAL ONCE",
        "!MODE ENDPOINT", "!ORDER COLUMN", "!TAG OFF", "!NVRAM CARCOL 100", "!READTYPE FLU", "!READ", "!STATUS", "!STATUS",
        "!STATUS", "!TRANSFER"]
    assert fluo["values"] == {"D4": float(default_value("flu", 520, 3, 4))} and fluo["gain"] is None
    del fake.commands[2:]
    lum = await run(hub, "read_luminescence", {"plate": "test_96_plate", "wells": ["A2"]})
    assert after_connect(fake)[:12] == [
        "!CLEAR DATA", "!READSTAGE TOP", "!XPOS 13.380 9.000 12", "!YPOS 12.240 9.000 8", "!STRIP 1 12", "!CSPEED 8",
        "!SHAKE OFF", "!AUTOPMT ON", "!EMWAVELENGTH 0", "!PMTCAL ONCE", "!MODE ENDPOINT", "!ORDER COLUMN"]
    assert "!READTYPE LUM" in fake.commands and "!FPW" not in " ".join(fake.commands)
    assert lum["values"] == {"A2": float(default_value("lum", 0, 0, 2))}
    with pytest.raises(InvalidArgument, match="allowed range"):  # integration time is not settable
        await hub.invoke("reader", "read_luminescence", {"plate": "test_96_plate", "integration_s": 5})
    await hub.stop()


@pytest.mark.parametrize("relative_columns", [False, True])
async def test_gemini_reads_only_the_rectangle_of_requested_wells(relative_columns):
    fake = FakeSpectraMax(model="gemini_em", relative_columns=relative_columns)
    hub = await start(fake)
    result = await run(hub, "read_fluorescence", {"plate": "test_96_plate", "excitation_nm": 485, "emission_nm": 520,
                                                  "wells": ["B2", "G7"]})
    assert after_connect(fake)[:22] == [
        "!CLEAR DATA", "!XPOS 13.380 9.000 12", "!YPOS 21.240 9.000 6", "!STRIP 2 6", "!CSPEED 8", "!SHAKE OFF", "!FPW 6",
        "!AUTOPMT ON", "!EXWAVELENGTH 485", "!EMWAVELENGTH 520", "!AUTOFILTER OFF", "!EMFILTER 7", "!TOPREADCLEAR ON",
        "!READSTAGE TOP", "!PMTCAL ON", "!MODE ENDPOINT", "!ORDER COLUMN", "!TAG OFF", "!WELLSCANMODE OFF", "!READTYPE FLU",
        "!READ", "!STATUS"]
    assert result["values"] == {"B2": float(default_value("flu", 520, 1, 2)), "G7": float(default_value("flu", 520, 6, 7))}
    del fake.commands[2:]
    lum = await run(hub, "read_luminescence", {"plate": "test_96_plate", "wells": ["A1"]})
    assert after_connect(fake)[:5] == ["!CLEAR DATA", "!TAG OFF", "!WELLSCANMODE OFF", "!XPOS 13.380 9.000 12", "!YPOS 12.240 9.000 1"]
    assert "!STRIP 1 1" in fake.commands and "!FPW 6" in fake.commands and "!READTYPE LUM" in fake.commands
    assert lum["values"] == {"A1": float(default_value("lum", 0, 0, 1))}
    await hub.stop()


async def test_gemini_emission_in_a_filter_gap_is_refused():
    hub = await start(FakeSpectraMax(model="gemini_em"))
    error = await failure(hub, "read_fluorescence", {"plate": "test_96_plate", "excitation_nm": 350, "emission_nm": 417})
    assert error["code"] == "invalid_argument" and "cutoff filter" in error["message"]
    await hub.stop()


async def test_saturated_and_unread_wells_are_left_out():
    fake = FakeSpectraMax()
    marks = {(0, 1): "#SAT", (0, 2): "-", (0, 3): "-1.5E-3", (0, 4): "2.5e1"}
    fake.value = lambda kind, nm, row, column: marks.get((row, column), "0.5")
    hub = await start(fake)
    result = await run(hub, "read_absorbance", {"plate": "test_96_plate", "wavelength_nm": 600, "wells": ["A1", "A2", "A3", "A4", "B1"]})
    assert result["values"] == {"A3": -0.0015, "A4": 25.0, "B1": 0.5}
    spectrum = await run(hub, "read_absorbance_spectrum", {"plate": "test_96_plate", "start_nm": 400, "end_nm": 410, "step_nm": 10,
                                                            "wells": ["A1", "A3"]})
    assert spectrum["values"] == {"A3": [-0.0015, -0.0015]}
    await hub.stop()


# -- drawer, temperature, shaking -------------------------------------------------------------------

async def test_drawer():
    fake = FakeSpectraMax()
    hub = await start(fake)
    assert (await run(hub, "status"))["tray_open"] is None  # no drawer query in the protocol
    assert (await run(hub, "open_tray"))["tray_open"] is True and fake.drawer_open
    with pytest.raises(NeedsAttention, match="tray is open"):
        await run(hub, "read_absorbance", {"plate": "test_96_plate", "wavelength_nm": 450})
    assert (await run(hub, "close_tray"))["tray_open"] is False and not fake.drawer_open
    assert fake.commands[-4:] == ["!OPEN", "!TEMP", "!CLOSE", "!TEMP"]
    await hub.stop()


@pytest.mark.parametrize("model", ["m5", "gemini_em"])
async def test_temperature(model):
    fake = FakeSpectraMax(model=model)
    hub = await start(fake)
    assert (await run(hub, "status"))["target_temperature_c"] is None
    with pytest.raises(InvalidArgument, match="allowed range"):
        await hub.invoke("reader", "set_temperature", {"celsius": 20})  # below what heating alone can hold
    assert (await run(hub, "set_temperature", {"celsius": 37}))["target_temperature_c"] == 37
    assert "!TEMP 37.0" in fake.commands
    assert (await run(hub, "wait_for_temperature"))["temperature_c"] == 37
    assert (await run(hub, "deactivate_temperature"))["target_temperature_c"] is None and "!TEMP 0" in fake.commands
    await hub.stop()


async def test_shake():
    fake = FakeSpectraMax()
    hub = await start(fake)
    assert hub.describe_device("reader")["limits"]["shake_modes"] == ["linear"]
    await run(hub, "shake", {"duration_s": 0.01, "mode": "linear"})
    assert fake.commands[-3:-1] == ["!SHAKE NOW", "!SHAKE STOP"] and not fake.shaking
    with pytest.raises(InvalidArgument, match="not settable"):
        await hub.invoke("reader", "shake", {"duration_s": 1, "mode": "linear", "rpm": 300})
    await hub.stop()


# -- errors and framing robustness ------------------------------------------------------------------

async def test_instrument_errors_surface_as_pyreagent_errors():
    fake = FakeSpectraMax()
    hub = await start(fake)
    args = {"plate": "test_96_plate", "wavelength_nm": 450}
    fake.fail["!READ"] = 110
    assert (await failure(hub, "read_absorbance", args))["code"] == "needs_attention"
    fake.trailing_fail["!WAVELENGTH"] = 101  # OK, then FAIL: caught by the linger after the expected field
    assert (await failure(hub, "read_absorbance", args))["code"] == "invalid_argument"
    fake.fail["!READ"] = 400
    error = await failure(hub, "read_absorbance", args)
    assert error["code"] == "hardware_error" and error["details"]["error_code"] == 400
    assert hub.describe_device("reader")["state"] == "error"
    await run(hub, "reset")
    assert fake.commands[-3:] == ["!", "!OPTION", "!CLEAR ERROR"] and hub.describe_device("reader")["state"] == "idle"
    await hub.stop()


async def test_wrong_field_counts_do_not_corrupt_later_replies():
    fake = FakeSpectraMax()
    fake.extra_field = {"!SPEED", "!CALIBRATE"}            # more fields than the table says
    fake.drop_second_field = {"!READTYPE ABSPLA", "!TEMP 30.0"}  # fewer, where the second field carries nothing needed
    fake.warning = "Warning: lamp warming up"
    hub = await start(fake, extra_field_timeout_s=0.01)
    result = await run(hub, "read_absorbance", {"plate": "test_96_plate", "wavelength_nm": 450, "wells": ["A1"]})
    assert result["values"] == {"A1": float(default_value("abs", 450, 0, 1))}
    await run(hub, "set_temperature", {"celsius": 30})
    await hub.stop()


async def test_cancelling_a_read_sends_cancel():
    fake = FakeSpectraMax(busy_polls=10**6)
    backend = SpectraMaxBackend(transport=MockTransport(fake), poll_interval_s=0.001)
    hub = DeviceHub([PlateReader("reader", backend, labware=CATALOG)])
    await hub.start()
    accepted = await hub.invoke("reader", "read_absorbance", {"plate": "test_96_plate", "wavelength_nm": 450})
    while fake.commands.count("!STATUS") < 3:
        await asyncio.sleep(0.001)
    assert (await run(hub, "status"))["temperature_c"] is None  # served from cache: the reader is measuring
    await hub.jobs.cancel(accepted.job.id)
    assert (await hub.jobs.wait(accepted.job.id, 5)).status == "cancelled"
    assert fake.commands[-1] == "!CANCEL" and fake.cancelled and "!TEMP" not in fake.commands
    await hub.stop()


# -- what each model offers -------------------------------------------------------------------------

async def test_capabilities_per_model():
    expected = {"m5": ["absorbance", "absorbance_spectrum", "fluorescence", "luminescence", "temperature", "shaking", "tray"],
                "384_plus": ["absorbance", "absorbance_spectrum", "temperature", "shaking", "tray"],
                "gemini_em": ["fluorescence", "luminescence", "temperature", "shaking", "tray"]}
    for model, features in expected.items():
        hub = await start(FakeSpectraMax(model=model))
        described = hub.describe_device("reader")
        assert described["limits"]["features"] == features
        assert described["limits"]["plates"] == ["test_96_plate"]  # plates need geometry
        assert described["limits"]["min_temperature_c"] == 25 and described["limits"]["max_temperature_c"] == 45
        await hub.stop()
    plus = await start(FakeSpectraMax(model="384_plus"))
    assert "read_fluorescence" not in {c["name"] for c in plus.describe_device("reader")["capabilities"]}
    with pytest.raises(NotSupported):
        await plus.invoke("reader", "read_fluorescence", {"plate": "test_96_plate", "excitation_nm": 485, "emission_nm": 520})
    with pytest.raises(NotSupported):
        await plus.invoke("reader", "read_luminescence", {"plate": "test_96_plate"})
    with pytest.raises(InvalidArgument, match="geometry"):
        await plus.invoke("reader", "read_absorbance", {"plate": "generic_96_wellplate", "wavelength_nm": 450})
    await plus.stop()
    gemini = await start(FakeSpectraMax(model="gemini_em"))
    with pytest.raises(NotSupported):
        await gemini.invoke("reader", "read_absorbance", {"plate": "test_96_plate", "wavelength_nm": 450})
    limits = gemini.describe_device("reader")["limits"]
    assert (limits["min_emission_nm"], limits["optics"], limits["max_gain"]) == (360, ["top", "bottom"], None)
    await gemini.stop()


def test_options_are_checked():
    with pytest.raises(ValueError, match="model must be one of"):
        SpectraMaxBackend(port="COM1", model="m2")  # type: ignore[arg-type]
