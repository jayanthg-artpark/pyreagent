"""Agilent BioTek driver: protocol vectors from the spec, and every capability end-to-end against a protocol fake."""

from __future__ import annotations

import asyncio
import math

import pytest

from fake_biotek import FakeBioTek, FakeLink, counts, ht_od, od
from pyreagent.core import CommunicationError, DeviceHub, InvalidArgument, NeedsAttention, NotSupported
from pyreagent.core.errors import DeviceTimeout, HardwareError
from pyreagent.devices.plate_reader import PlateReader
from pyreagent.devices.plate_reader.biotek import BioTekBackend
from pyreagent.devices.plate_reader.biotek import protocol as p
from pyreagent.io import MockTransport, TcpTransport
from pyreagent.labware import LabwareGeometry

PLATE = "nest_96_wellplate_200ul_flat"  # has geometry
WELLS_96 = [(r, c) for r in range(1, 9) for c in range(1, 13)]


def label(row: int, column: int) -> str:
    return f"{'ABCDEFGHIJKLMNOP'[row - 1]}{column}"


async def make(model: str = "synergy_h1", fake: FakeBioTek | None = None, **options) -> tuple[DeviceHub, BioTekBackend, FakeBioTek]:
    fake = fake or FakeBioTek(model)
    backend = BioTekBackend(model=model, transport=FakeLink(fake), **options)
    backend.ack_only_pause_s, backend.drain_poll_s = 0, 0.005
    hub = DeviceHub([PlateReader("reader", backend, poll_interval_s=0.01)])
    await hub.start()
    return hub, backend, fake


async def job(hub: DeviceHub, capability: str, args: dict | None = None):
    accepted = await hub.invoke("reader", capability, args or {})
    if accepted.job is None:
        return accepted.result
    return await hub.jobs.wait(accepted.job.id, 10)


async def run(hub: DeviceHub, capability: str, args: dict | None = None) -> dict:
    done = await job(hub, capability, args)
    if not hasattr(done, "status"):
        return done
    assert done.status == "succeeded", done.error
    return done.result


# -- protocol vectors ---------------------------------------------------------------------------------

def test_checksum_and_status_words():
    assert p.checksum(b"\x01\xff\x02") == 2
    assert p.split_status(b"2360000\x03") == (b"236", 0)
    assert p.split_status(b"237002A\x03") == (b"237", 0x2A)
    assert p.split_status(b"196804  \x1e000\x03", power_on_flag=True) == (b"196804  ", 0)
    assert p.split_status(b"\x1e001\x03", power_on_flag=True)[1] == 1
    for bad, flag in ((b"\x1f000\x03", True), (b"\x1e000\x03", False), (b"00G0\x03", False), (b"0000", False)):
        with pytest.raises(CommunicationError):
            p.split_status(bad, power_on_flag=flag)


def test_identity_capability_and_temperature():
    assert p.parse_identity(b"7090202  Version 2.24   ") == ("7090202", "2.24")
    assert p.parse_identity(b"1650200  Version 1.04   ") == ("1650200", "1.04")
    assert p.parse_capability(b"0" * 17) == (False, "0" * 16)
    assert p.parse_capability(b"122,50,12,70,0000") == (True, "22,50,12,70,0000")
    with pytest.raises(CommunicationError):
        p.parse_capability(b"2" + b"0" * 16)
    assert p.parse_temperature(b"236") == 23.6 and p.parse_temperature(b"999") is None and p.parse_temperature(b"111") is None


@pytest.mark.parametrize(("caps", "rect", "expected"), [
    ((0, 1), (1, 1, 8, 12), "004701010108120001200100001100100000106000080580113"),
    ((0, 0), (1, 1, 1, 1), "00460101010101000120010000110010000010000080580050"),
    ((0, 1), (1, 1, 1, 1), "004701010101010001200100001100100000106000080580104"),
    ((1, 0), (1, 1, 1, 1), "00490101010101000120010000110010000010000080000580194"),
    ((1, 1), (1, 1, 1, 1), "005001010101010001200100001100100000106000080000580248"),
    ((1, 1), (5, 7, 8, 12), "005001050708120001200100001100100000106000080000580011"),
])
def test_absorbance_records(caps, rect, expected):
    body = p.absorbance_body(rect, 580, extra_field=bool(caps[0]), extended_optics=bool(caps[1]))
    assert p.record(body) == expected.encode()
    assert p.record(body, etx=True) == expected.encode() + b"\x03"


def test_fluorescence_luminescence_shake_and_focus_records():
    assert p.record(p.fluorescence_body((1, 1, 8, 12), 485, 528)) == \
        b"0084010101081200012001000011001000001350001002002000485000052800000000000000000021001119"
    lum = [p.record(p.luminescence_body(rect, 0.4)).decode() for rect in p.cover({(1, 1)} | {(r, c) for r in range(2, 8) for c in (1, 2, 3)}
                                                                                       | {(r, 4) for r in (4, 5, 6)})]
    assert lum[0] == "008401010107010001200100001100100000123000020200200-001000-00300000000000000000001351086"
    assert lum[1][4:14] == "0102020703" and lum[1].endswith("1351090")
    assert lum[2][4:14] == "0104040604" and lum[2].endswith("1351094")
    assert p.luminescence_body((1, 1, 1, 1), 19.8)[33:41] == "23009540"
    with pytest.raises(ValueError):
        p.luminescence_body((1, 1, 1, 1), 20.0)
    assert p.record(p.shake_body(960, orbital=True, amplitude_mm=3)) == b"0033010101010100002000000013960130190"
    assert p.focus(p.FOCUS_FLUORESCENCE, 7.5) == b"621720\x03"
    assert p.focus(p.FOCUS_LUMINESCENCE, 4.5) == b"318720\x03"


def test_well_cover():
    cells = {(1, 1)} | {(r, c) for r in range(2, 8) for c in (1, 2, 3)} | {(r, 4) for r in (4, 5, 6)}
    assert p.cover(cells) == [(1, 1, 7, 1), (2, 2, 7, 3), (4, 4, 6, 4)]
    assert p.bounding({(2, 3), (3, 4)}) == (2, 3, 3, 4)


def geometry(rows: int, columns: int, *, a1_x: float, a1_from_back: float, last_x: float, last_from_back: float,
             size_x: float, size_y: float, height: float) -> LabwareGeometry:
    """A pyreagent geometry (origin front-left) built from back-edge measurements."""
    return LabwareGeometry(size_x_mm=size_x, size_y_mm=size_y, size_z_mm=height, a1_x_mm=a1_x, a1_y_mm=size_y - a1_from_back,
                           pitch_x_mm=(last_x - a1_x) / (columns - 1), pitch_y_mm=(last_from_back - a1_from_back) / (rows - 1),
                           well_bottom_z_mm=1, well_depth_mm=10)


def test_plate_geometry_conversion():
    gen5 = geometry(8, 12, a1_x=14.35, a1_from_back=11.22, last_x=113.53, last_from_back=74.34, size_x=127.88, size_y=85.59, height=14.22)
    assert p.geometry(8, 12, gen5) == b"08120112207434014351135308559127881422\x03"
    cellvis96 = geometry(8, 12, a1_x=14.30, a1_from_back=11.39, last_x=113.30, last_from_back=74.39, size_x=127.60, size_y=85.75,
                         height=13.83)
    assert p.geometry(8, 12, cellvis96) == b"08120113907439014301133008575127601383\x03"
    cellvis24 = geometry(4, 6, a1_x=16.72, a1_from_back=14.01, last_x=113.22, last_from_back=71.91, size_x=127.50, size_y=85.25,
                         height=20.00)
    assert p.geometry(4, 6, cellvis24) == b"04060140107191016721132208525127502000\x03"  # rounded, not truncated


def test_record_reply_and_result_frames():
    data, status = p.split_status(b"0350000000000000010000000000490300000\x03")
    assert status == 0 and p.parse_record_reply(data) == "000000000000001000000000049"
    with pytest.raises(CommunicationError, match="checksum"):
        p.parse_record_reply(b"035000000000000001000000000049031")
    with pytest.raises(CommunicationError, match="length"):
        p.parse_record_reply(b"036000000000000001000000000049030")

    frame, _ = p.split_status(b"01,1,\r000:00:00.0,228,01,01,+0.1917\r\n228\x1a088\x1a0000\x03")
    result = p.parse_result(frame)
    assert result.values == {(1, 1): 0.1917} and result.temperature_c == 22.8
    frame, _ = p.split_status(b"01,1,\r000:00:00.0,237,04,04,0000018\r\n,05,04,0000017\r\n,06,04,0000014\r\n237\x1a210\x1a0000\x03")
    assert p.parse_result(frame).values == {(4, 4): 18.0, (5, 4): 17.0, (6, 4): 14.0}
    bad = frame.replace(b"210", b"211")
    with pytest.raises(CommunicationError, match="checksum"):
        p.parse_result(bad)
    assert p.parse_result(bad, verify_checksum=False).values[(6, 4)] == 14.0
    stars = b"01,1,\r000:00:00.0,228,01,01,*******\r\n228\x1a"
    stars += f"{p.checksum(stars[6:])}".encode() + b"\x1a"
    assert math.isnan(p.parse_result(stars).values[(1, 1)])


def test_ht_setup_packet():
    expected = (b"\x36" + b"\x00" * 7 + b"0" + b"\x00" * 20 + b"\x10" + b"\x00" * 19 + b"000" + b"\x00" * 14 + b"600" + b"000" * 5
                + b"\x00" * 86)
    assert p.ht_setup_packet(600) == expected
    shaken = p.ht_setup_packet(600, shake_s=10, shake_intensity=2)
    assert shaken[30] == 0x10 and shaken[162:165] == b"\x0a\x00\x02" and shaken[0] == p.checksum(shaken[1:])
    assert p.ht_setup_packet(340, shake_s=1234, shake_intensity=3)[0] == 0x20
    assert p.ht_setup_packet(340, shake_s=1234, shake_intensity=3)[162:165] == b"\xd2\x04\x03"
    assert p.ht_setup_packet(600, shake_s=65535, shake_intensity=3)[0] == 0x47


def test_ht_result_frame():
    body = b"012:34:56.7,+1000,+2000\r\n,+3000,+4000\r\n237\x1a"
    frame = b"\r" + body + bytes([p.checksum(body)]) + b"\x1a0000\x03"
    assert len(frame) == p.ht_frame_length(2, 2) == 51
    assert p.parse_ht_result(frame, 2, 2) == ([[1.0, 2.0], [3.0, 4.0]], 23.7)
    over = frame.replace(b",+2000", b",*****")
    over = over[:-7] + bytes([p.checksum(over[1:-7])]) + over[-6:]
    assert math.isnan(p.parse_ht_result(over, 2, 2)[0][0][1])
    with pytest.raises(CommunicationError, match="checksum"):
        p.parse_ht_result(frame[:-7] + bytes([(frame[-7] + 1) & 0xFF]) + frame[-6:], 2, 2)
    assert p.ht_frame_length(4, 6) == 175


async def test_transports_read_exactly():
    mock = MockTransport(lambda data: b"\x06\x03\x1a\x03rest")
    await mock.open()
    await mock.write(b"z")
    assert await mock.read_exactly(3, 1) == b"\x06\x03\x1a"
    with pytest.raises(TimeoutError):
        await mock.read_exactly(10, 1)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"\x03\x03ab")
        await writer.drain()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    tcp = TcpTransport("127.0.0.1", server.sockets[0].getsockname()[1])
    await tcp.open()
    assert await tcp.read_exactly(3, 2) == b"\x03\x03a"
    await tcp.close()
    server.close()


# -- connect ------------------------------------------------------------------------------------------

async def test_connect_rdop22():
    hub, backend, fake = await make("synergy_h1")
    assert backend.transport.sent[:3] == [b"e", b"z", b"2000\x03"]
    info = hub.describe_device("reader")["driver"]
    assert info["model"] == "Synergy H1" and info["basecode"] == "1650200" and info["version"] == "1.04"
    await hub.stop()


async def test_connect_falls_back_to_38461_baud():
    fake = FakeBioTek("cytation5", baudrate=38461)
    backend = BioTekBackend(model="cytation5", transport=FakeLink(fake))
    await backend.connect()
    assert backend.transport.settings["baudrate"] == 38461 and backend.transport.opened == 2
    assert backend.identity["basecode"] == "1650200"
    await backend.disconnect()

    no_fallback = BioTekBackend(model="cytation5", transport=FakeLink(FakeBioTek("cytation5", baudrate=38461)), fallback_baudrate=None)
    with pytest.raises(DeviceTimeout):
        await no_fallback.connect()


async def test_connect_ht_drains_stale_output_and_clears_power_on_flag():
    fake = FakeBioTek("synergy_ht")
    fake.queued += b"0000\x03\x067090202  Version 2.24   0000\x03"  # replies left over from an earlier session
    hub, backend, _ = await make("synergy_ht", fake)
    assert hub.device("reader").state.value == "idle"
    assert fake.log[:2] == [("e", b""), ("C", b"")]
    capabilities = [f"{n}\x03".encode() for n in (2000, 1013, 1006, 1008, 1009, 1012, 1020, 1031)]
    assert [param for command, param in fake.log if command == "z"] == capabilities
    assert fake.log[-1] == ("n", b"1") and not fake.power_on
    assert backend.identity["serial_number"] == "196804"
    limits = hub.describe_device("reader")["limits"]
    assert (limits["min_temperature_c"], limits["max_temperature_c"]) == (22, 50)
    await hub.stop()


@pytest.mark.parametrize(("reader", "driver", "caps"), [
    ("synergy_ht", "synergy_h1", None), ("synergy_h1", "synergy_ht", None), ("synergy_h1", "cytation1", {2000: "0"}),
])
async def test_connect_refuses_the_wrong_model(reader, driver, caps):
    backend = BioTekBackend(model=driver, transport=FakeLink(FakeBioTek(reader, caps=caps)))
    backend.drain_poll_s = 0.005
    with pytest.raises(NeedsAttention, match="Synergy HT"):
        await backend.connect()
    assert not backend.transport.is_open


# -- reads (H1 / Cytation) ----------------------------------------------------------------------------

async def test_absorbance_full_plate():
    hub, backend, fake = await make("synergy_h1")
    result = await run(hub, "read_absorbance", {"plate": PLATE, "wavelength_nm": 580})
    assert result["values"] == {label(r, c): od(r, c) for r, c in WELLS_96}
    assert fake.records == ["01010108120001200100001100100000106000080580"]  # caps 2056=0, 2064=1 (spec vector 10)
    assert fake.geometry is not None and fake.geometry.startswith(b"0812")
    await run(hub, "read_absorbance", {"plate": PLATE, "wavelength_nm": 450, "wells": ["H12"]})
    assert [c for c, _ in fake.log].count("y") == 1  # geometry cached while the tray stays shut
    assert fake.records[-1][2:10] == "08120812"
    await run(hub, "close_tray")
    await run(hub, "read_absorbance", {"plate": PLATE, "wavelength_nm": 450, "wells": ["A1"]})
    assert [c for c, _ in fake.log].count("y") == 2
    await hub.stop()


async def test_well_selection_reads_rectangles_and_keeps_requested_wells():
    hub, _, fake = await make("cytation5")
    wells = ["A1"] + [f"{r}{c}" for r in "BCDEFG" for c in (1, 2, 3)] + ["D4", "E4", "F4"]
    result = await run(hub, "read_luminescence", {"plate": PLATE, "wells": wells, "integration_s": 0.4})
    assert fake.focus == b"318720\x03"
    assert [b[2:10] for b in fake.records] == ["01010701", "02020703", "04040604"]
    assert all(b.endswith(p.LUM_TAIL) and b[33:41] == "23000020" for b in fake.records)
    assert result["values"] == {w: counts("ABCDEFGH".index(w[0]) + 1, int(w[1:])) for w in wells}
    await hub.stop()

    hub, _, fake = await make("cytation5", well_blocks="bounding")
    result = await run(hub, "read_absorbance", {"plate": PLATE, "wavelength_nm": 600, "wells": ["B3", "D7"]})
    assert [b[2:10] for b in fake.records] == ["02030407"] and set(result["values"]) == {"B3", "D7"}
    await hub.stop()


async def test_fluorescence():
    hub, backend, fake = await make("synergy_h1")
    result = await run(hub, "read_fluorescence", {"plate": PLATE, "excitation_nm": 485, "emission_nm": 528})
    assert fake.focus == b"621720\x03"  # default 7.5 mm
    assert p.record(fake.records[0]).endswith(b"21001119")
    assert result["gain"] is None and result["values"]["H12"] == counts(8, 12)
    await run(hub, "read_fluorescence", {"plate": PLATE, "excitation_nm": 485, "emission_nm": 528, "focal_height_mm": 10, "wells": ["A1"]})
    assert fake.focus == b"624220\x03"
    await hub.stop()


async def test_reader_checks():
    hub, _, fake = await make("synergy_h1")
    base = {"plate": PLATE, "excitation_nm": 485, "emission_nm": 528}
    with pytest.raises(InvalidArgument, match="gain automatically"):
        await hub.invoke("reader", "read_fluorescence", {**base, "gain": 50})
    with pytest.raises(InvalidArgument, match="top only"):
        await hub.invoke("reader", "read_fluorescence", {**base, "optics": "bottom"})
    with pytest.raises(InvalidArgument, match="focal_height_mm"):
        await hub.invoke("reader", "read_fluorescence", {**base, "focal_height_mm": 12})  # H1 tops out at 10.68
    with pytest.raises(InvalidArgument, match="allowed range"):
        await hub.invoke("reader", "read_absorbance", {"plate": PLATE, "wavelength_nm": 220})
    with pytest.raises(InvalidArgument, match="allowed range"):
        await hub.invoke("reader", "read_luminescence", {"plate": PLATE, "integration_s": 20})
    with pytest.raises(InvalidArgument, match="geometry"):
        await hub.invoke("reader", "read_absorbance", {"plate": "generic_96_wellplate", "wavelength_nm": 600})
    with pytest.raises(InvalidArgument, match="flash"):
        await hub.invoke("reader", "read_fluorescence", {**base, "flashes": 10})
    with pytest.raises(InvalidArgument, match="multiple of 0.2"):
        await hub.invoke("reader", "read_luminescence", {"plate": PLATE, "integration_s": 0.3})
    assert fake.records == []
    await hub.stop()


async def test_overflow_reads_as_nan():
    hub, _, fake = await make("synergy_h1")
    fake.overflow = {(1, 2)}
    result = await run(hub, "read_absorbance", {"plate": PLATE, "wavelength_nm": 600, "wells": ["A1", "A2"]})
    assert result["values"]["A1"] == od(1, 1) and math.isnan(result["values"]["A2"])
    await hub.stop()


# -- reads (Synergy HT) -------------------------------------------------------------------------------

async def test_ht_absorbance_sequence():
    hub, backend, fake = await make("synergy_ht")
    sent_before = len(backend.transport.sent)
    result = await run(hub, "read_absorbance", {"plate": "generic_96_wellplate", "wavelength_nm": 600, "wells": ["B3", "C4"]})
    assert backend.transport.sent[sent_before:] == [
        b"V", p.ht_setup_packet(600), b"{", b"\x02", b"i", b"1", b"r", b"\x02\x03", b"%", b"\x03\x04", b"k", b"0", b"Q", b"1",
        b"t", b"40000\x03", b"S"]
    assert result["values"] == {"B3": ht_od(2, 3), "C4": ht_od(3, 4)}  # B4 and C3 measured and dropped
    full = await run(hub, "read_absorbance", {"plate": "generic_24_wellplate", "wavelength_nm": 450})
    assert fake.ht_setup["{"] == b"\x00" and len(full["values"]) == 24 and full["values"]["D6"] == ht_od(4, 6)
    await hub.stop()


async def test_ht_model_differences():
    hub, _, _ = await make("synergy_ht")
    described = hub.describe_device("reader")
    names = {c["name"] for c in described["capabilities"]}
    assert {"read_absorbance", "set_temperature", "open_tray"} <= names
    assert not names & {"read_fluorescence", "read_luminescence", "read_absorbance_spectrum", "shake"}
    assert described["limits"]["plate_formats"] == ["4x6", "8x12"]
    with pytest.raises(NotSupported):
        await hub.invoke("reader", "read_fluorescence", {"plate": PLATE, "excitation_nm": 485, "emission_nm": 528})
    with pytest.raises(NotSupported):
        await hub.invoke("reader", "shake", {"duration_s": 5})
    with pytest.raises(InvalidArgument, match="not a plate format"):
        await hub.invoke("reader", "read_absorbance", {"plate": "generic_384_wellplate", "wavelength_nm": 600})
    await hub.stop()


async def test_ht_late_frame_is_drained_after_reconnect():
    hub, backend, fake = await make("synergy_ht", read_timeout_s=0.05)
    fake.ignore_abort, fake.result_delay_s = True, 0.2
    failed = await job(hub, "read_absorbance", {"plate": "generic_96_wellplate", "wavelength_nm": 600, "wells": ["A1"]})
    assert failed.error["code"] == "timeout" and fake.aborts == 1
    await backend.disconnect()
    await asyncio.sleep(0.3)  # the frame is produced while the port is closed: the reader holds it
    assert fake.queued
    await backend.connect()
    fake.result_delay_s = 0.01
    result = await run(hub, "read_absorbance", {"plate": "generic_96_wellplate", "wavelength_nm": 600, "wells": ["A1"]})
    assert result["values"] == {"A1": ht_od(1, 1)}
    await hub.stop()


# -- tray, temperature, shaking -----------------------------------------------------------------------

async def test_tray():
    hub, _, fake = await make("synergy_h1")
    assert (await run(hub, "status"))["tray_open"] is None
    assert (await run(hub, "open_tray"))["tray_open"] is True and fake.tray_open
    assert (await run(hub, "close_tray"))["tray_open"] is False and not fake.tray_open
    assert fake.tray_speed == b"S0" and [c for c, _ in fake.log].count("&") == 1
    await hub.stop()

    hub, _, fake = await make("synergy_h1", fake=FakeBioTek(caps={2138: "0"}), slow_tray=True)
    with pytest.raises(NotSupported, match="slowly"):
        await hub.invoke("reader", "open_tray")
    await hub.stop()
    hub, _, fake = await make("synergy_ht")
    await run(hub, "open_tray")
    assert fake.tray_open and fake.tray_speed is None
    await hub.stop()


async def test_temperature_rdop22():
    hub, _, fake = await make("synergy_h1")
    status = await run(hub, "set_temperature", {"celsius": 37})
    assert fake.setpoint == b"37000" and status == {"tray_open": None, "temperature_c": 23.6, "target_temperature_c": 37}
    with pytest.raises(InvalidArgument, match="only heat"):
        await hub.invoke("reader", "set_temperature", {"celsius": 21})
    with pytest.raises(InvalidArgument, match="allowed range"):
        await hub.invoke("reader", "set_temperature", {"celsius": 50})
    assert (await run(hub, "deactivate_temperature"))["target_temperature_c"] is None and fake.setpoint is None
    fake.temperature_tenths = 999
    assert (await run(hub, "status"))["temperature_c"] is None
    await hub.stop()

    hub, _, _ = await make("cytation5")
    with pytest.raises(NotSupported):
        await hub.invoke("reader", "set_temperature", {"celsius": 37})
    await hub.stop()
    hub, _, fake = await make("cytation1", temperature_range_c=(4, 45), can_cool=True)
    await run(hub, "set_temperature", {"celsius": 10})
    assert fake.setpoint == b"10000"
    await hub.stop()


async def test_temperature_ht():
    hub, _, fake = await make("synergy_ht")
    await run(hub, "set_temperature", {"celsius": 37})
    assert fake.setpoint == b"37"
    with pytest.raises(InvalidArgument, match="whole"):
        await hub.invoke("reader", "set_temperature", {"celsius": 37.5})
    with pytest.raises(InvalidArgument, match="allowed range"):
        await hub.invoke("reader", "set_temperature", {"celsius": 60})
    await run(hub, "deactivate_temperature")
    assert ("g", b" 0") in fake.log and fake.setpoint is None
    await hub.stop()


async def test_shake():
    hub, backend, fake = await make("synergy_h1")
    waited: list[float] = []

    async def no_wait(seconds: float) -> None:
        waited.append(seconds)
    backend._wait = no_wait
    await run(hub, "shake", {"duration_s": 30, "mode": "orbital"})
    assert p.record(fake.records[-1]) == b"0033010101010100002000000013030130" + f"{p.checksum(fake.records[-1]):03d}".encode()
    await run(hub, "shake", {"duration_s": 2000, "mode": "linear"})
    assert [r[24:29] for r in fake.records[1:]] == ["96003", "96003", "08003"] and waited[1:] == [960, 960, 80]
    with pytest.raises(InvalidArgument, match="not settable"):
        await hub.invoke("reader", "shake", {"duration_s": 5, "rpm": 500})
    with pytest.raises(InvalidArgument, match="shakes linear, orbital only"):
        await hub.invoke("reader", "shake", {"duration_s": 5, "mode": "double_orbital"})
    await hub.stop()


async def test_cancelling_a_shake_aborts_it():
    hub, _, fake = await make("synergy_h1")
    accepted = await hub.invoke("reader", "shake", {"duration_s": 60})
    await asyncio.sleep(0.1)
    await hub.jobs.cancel(accepted.job.id)
    assert (await hub.jobs.wait(accepted.job.id, 5)).status == "cancelled"
    assert fake.aborts == 1
    assert (await run(hub, "status"))["temperature_c"] == 23.6  # the line was drained and works again
    await hub.stop()


# -- errors -------------------------------------------------------------------------------------------

async def test_nak_and_error_status():
    hub, _, fake = await make("synergy_h1")
    fake.nak.add("J")
    with pytest.raises(CommunicationError, match="rejected"):
        await hub.invoke("reader", "open_tray")
    fake.nak.clear()
    fake.fail["A"] = 0x0A01
    with pytest.raises(HardwareError, match="0x0A01") as err:
        await hub.invoke("reader", "close_tray")
    assert err.value.details == {"status": "0A01"}
    assert hub.device("reader").state.value == "error"
    await hub.stop()


@pytest.mark.parametrize("model", ["synergy_h1", "synergy_ht"])
async def test_measurement_errors(model):
    plate = PLATE if model != "synergy_ht" else "generic_96_wellplate"
    read = {"plate": plate, "wavelength_nm": 600, "wells": ["A1"]}
    hub, _, fake = await make(model)
    fake.corrupt_result = True
    assert (await job(hub, "read_absorbance", read)).error["code"] == "communication_error"
    fake.corrupt_result = False
    fake.measure_error = 0x2A
    failed = await job(hub, "read_absorbance", read)
    assert failed.error["code"] == "hardware_error" and failed.error["details"] == {"status": "002A"}
    await hub.stop()


async def test_read_timeout_and_cancel_abort_the_read():
    hub, _, fake = await make("synergy_h1", read_timeout_s=0.05)
    fake.hang = True
    read = {"plate": PLATE, "wavelength_nm": 600, "wells": ["A1"]}
    assert (await job(hub, "read_absorbance", read)).error["code"] == "timeout"
    assert fake.aborts == 1
    fake.hang = False
    assert (await run(hub, "read_absorbance", read))["values"] == {"A1": od(1, 1)}
    await hub.stop()

    hub, _, fake = await make("synergy_h1")
    fake.result_delay_s = 5
    await run(hub, "status")
    fake.temperature_tenths = 300
    accepted = await hub.invoke("reader", "read_absorbance", read)
    await asyncio.sleep(0.1)
    assert (await run(hub, "status"))["temperature_c"] == 23.6  # status during a read uses the last known value
    await hub.jobs.cancel(accepted.job.id)
    assert (await hub.jobs.wait(accepted.job.id, 5)).status == "cancelled"
    assert fake.aborts == 1
    await hub.stop()
