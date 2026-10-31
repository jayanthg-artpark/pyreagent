"""BMG Labtech CLARIOstar wire format (FTDI serial line, firmware 1.x).

Frame: ``02 | size u16 BE (whole frame) | 0C | payload | checksum u24 BE | 0D``; the checksum is
the plain byte sum of everything before it. Requests start with a command family byte; replies
start with a reply type, then five status bytes. All integers are big-endian except the stored
parameter table, which is little-endian. Pure functions only: no I/O here.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Iterable

STX, CR, READER_ID = 0x02, 0x0D, 0x0C
FRAME_OVERHEAD = 8  # STX, size (2), reader id, checksum (3), CR
HEAD_SIZE = 3  # STX + size: enough to know how much more to read

# Request families.
INITIALIZE, DRAWER, RUN, REQUEST, TEMPERATURE, STOP, SET_PARAMETER, STATUS = 0x01, 0x03, 0x04, 0x05, 0x06, 0x0B, 0x0E, 0x80
FORBIDDEN_FAMILIES = frozenset({SET_PARAMETER})
"""Never sent: 0x0E writes the reader's stored (EEPROM) settings, and its format is unconfirmed."""

# Reply types (payload byte 0).
REPLY_STATUS, REPLY_DATA, REPLY_RUN_ACK, REPLY_PARAMETERS, REPLY_FIRMWARE, REPLY_INIT = 0x01, 0x02, 0x03, 0x07, 0x0A, 0x13

# Data-reply layout codes (payload byte 6).
LAYOUT_ABSORBANCE, LAYOUT_FLUORESCENCE, LAYOUT_LUMINESCENCE = 0xA9, 0xA1, 0x25

# Measurement modes (pre-separator byte 0) and its other bits.
MODE_FLUORESCENCE, MODE_LUMINESCENCE, MODE_ABSORBANCE = 0x00, 0x01, 0x02
BOTTOM_OPTIC, EDR = 0x40, 0x40

SCAN_DEFAULT = 0x0A  # bidirectional, top-left start, column-major (bit 3), bit 1 always set
SCAN_UNIDIRECTIONAL = 0x80
SEPARATOR = bytes.fromhex("270f270f")
MAX_WELLS = 384  # the well mask is 48 bytes

INIT_REQUEST = bytes.fromhex("0100001003")
STATUS_REQUEST = bytes([STATUS])
STOP_REQUEST = bytes([STOP, 0x00])
TEMPERATURE_OFF, TEMPERATURE_MONITOR = 0x0000, 0x0001


class FrameError(ValueError):
    """A received frame is malformed (bad start/end byte, reader id, length or checksum)."""


class ForbiddenCommand(ValueError):
    """The driver tried to build a command that must never reach the reader."""


# -- framing ------------------------------------------------------------------------------------------

def checksum(data: bytes) -> int:
    return sum(data) & 0xFFFFFF


def encode(payload: bytes) -> bytes:
    """Wrap a request payload in a frame. Refuses SET_PARAMETER (0x0E), which writes EEPROM."""
    if not payload:
        raise ValueError("empty payload")
    if payload[0] in FORBIDDEN_FAMILIES:
        raise ForbiddenCommand(f"command family 0x{payload[0]:02X} writes the reader's stored settings and is never sent")
    size = len(payload) + FRAME_OVERHEAD
    if size > 0xFFFF:
        raise ValueError(f"payload of {len(payload)} bytes does not fit a frame")
    head = bytes([STX]) + size.to_bytes(2, "big") + bytes([READER_ID]) + payload
    return head + checksum(head).to_bytes(3, "big") + bytes([CR])


def frame_size(head: bytes) -> int:
    """Total frame length from its first three bytes (STX + size)."""
    if len(head) < HEAD_SIZE or head[0] != STX:
        raise FrameError(f"frame does not start with STX: {head.hex()}")
    size = int.from_bytes(head[1:3], "big")
    if size < FRAME_OVERHEAD:
        raise FrameError(f"frame size {size} is shorter than the framing itself")
    return size


def decode(frame: bytes) -> bytes:
    """The payload of a complete frame, after checking it in the order the spec gives."""
    if len(frame) < FRAME_OVERHEAD or frame[0] != STX:
        raise FrameError(f"bad start byte or too short ({len(frame)} bytes)")
    if frame[-1] != CR:
        raise FrameError(f"bad end byte 0x{frame[-1]:02X}")
    if frame[3] != READER_ID:
        raise FrameError(f"bad reader id 0x{frame[3]:02X}")
    if int.from_bytes(frame[1:3], "big") != len(frame):
        raise FrameError(f"size field {int.from_bytes(frame[1:3], 'big')} but {len(frame)} bytes")
    if checksum(frame[:-4]) != int.from_bytes(frame[-4:-1], "big"):
        raise FrameError("checksum mismatch")
    return frame[4:-4]


# -- simple commands ------------------------------------------------------------------------------------

def request(command: int) -> bytes:
    """REQUEST family: 0x02 data, 0x07 stored parameters, 0x09 firmware info. Always 5 zero bytes after."""
    return bytes([REQUEST, command]) + bytes(5)


REQUEST_DATA, REQUEST_PARAMETERS, REQUEST_FIRMWARE = request(0x02), request(0x07), request(0x09)


def drawer(open_: bool) -> bytes:
    return bytes([DRAWER, 0x01 if open_ else 0x00]) + bytes(4)


def temperature(value: int) -> bytes:
    """0 = off, 1 = monitor only, otherwise a set point in 0.1 °C."""
    return bytes([TEMPERATURE]) + struct.pack(">H", value)


def setpoint(celsius: float) -> int:
    value = round(celsius * 10)
    if value <= TEMPERATURE_MONITOR:
        raise ValueError("set point too low to encode")  # 0 and 1 mean off / monitor
    return value


# -- status -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Status:
    """Status flags. Bytes 1-5 are carried by every reply; the rest only by a full STATUS reply."""

    busy: bool
    refused: bool
    warning: bool
    data_ready: bool
    drawer_open: bool
    plate_detected: bool
    motors_enabled: bool
    codes: tuple[int, ...] = ()
    """Quit codes (bytes 6-10) when refused or warning; byte 6 is the running family while busy."""
    running: int | None = None
    bottom_c: float | None = None
    top_c: float | None = None

    @classmethod
    def decode(cls, payload: bytes) -> "Status":
        if len(payload) < 6:
            raise FrameError(f"reply of {len(payload)} bytes has no status")
        b1, b2, b3 = payload[1], payload[2], payload[3]
        flagged = bool(b1 & 0x90)
        codes = tuple(c for c in payload[6:11]) if flagged and len(payload) >= 11 else ()
        running = payload[6] if b1 & 0x20 and not flagged and len(payload) > 6 else None
        bottom = top = None
        if len(payload) >= 15:
            bottom, top = (v / 10 if v else None for v in struct.unpack_from(">HH", payload, 11))
        return cls(busy=bool(b1 & 0x20), refused=bool(b1 & 0x10), warning=bool(b1 & 0x80), data_ready=bool(b2 & 0x01),
                   drawer_open=bool(b3 & 0x01), plate_detected=bool(b3 & 0x02), motors_enabled=bool(b3 & 0x20),
                   codes=codes, running=running, bottom_c=bottom, top_c=top)

    @property
    def quit_code(self) -> int | None:
        return self.codes[0] if self.codes else None

    @property
    def temperature_c(self) -> float | None:
        values = [t for t in (self.bottom_c, self.top_c) if t is not None]
        return round(sum(values) / len(values), 2) if values else None


QUIT_CODES: dict[int, str] = {
    1: "The reader received a frame with a bad checksum.",
    2: "The reader received a frame with a bad end byte.",
    3: "The reader received a frame with a bad length.",
    55: "The focal height is invalid.",
    65: "The kinetic cycle time is too short.",
    102: "No new measurement data yet.",
    110: "The target temperature is out of range for this reader.",
    201: "The reader is not initialized.",
    202: "The reader is busy.",
    213: "No measurement results are available.",
    214: "No measurement is running.",
    242: "The well-plate sensor failed or is unreadable.",
    243: "No plate is inserted, or the focal height is too low for the plate.",
    250: "The motor controller reported a collision.",
}


# -- startup replies ----------------------------------------------------------------------------------------

@dataclass
class Hardware:
    """What the INIT reply and the stored parameters say is fitted (firmware 1.300-1.999 only)."""

    firmware: int | None = None
    """Version x1000 (1350 = 1.35)."""
    build: int | None = None
    board_revision: int | None = None
    extended_red: bool = False
    dual_pmt: bool = False
    crossover: bool = False
    incubator: bool = True
    spectrometer: bool = True
    monochromators: bool = True
    filter_slides: str = ""
    parameters: dict[int, int] = field(default_factory=dict)

    @property
    def plus(self) -> bool:
        return (self.board_revision or 0) >= 2


def decode_init(payload: bytes, hardware: Hardware) -> None:
    """Fill ``hardware`` from the INIT reply's module blocks: ``[id u8][length u16][data]`` from byte 6."""
    i = 6
    while i + 3 <= len(payload):
        block, length = payload[i], int.from_bytes(payload[i + 1:i + 3], "big")
        data = payload[i + 3:i + 3 + length]
        if len(data) < length:
            raise FrameError(f"INIT block 0x{block:02X} is truncated")
        i += 3 + length
        if not data:
            continue
        if block == 0x01 and length >= 5:
            hardware.firmware, hardware.build, hardware.board_revision = int.from_bytes(data[0:2], "big"), data[2], data[4]
        elif block == 0x09:
            bit1, bit2 = bool(data[0] & 0x02), bool(data[0] & 0x04)
            hardware.extended_red, hardware.dual_pmt, hardware.crossover = bit1 or bit2, bit2, bit1 and bit2
        elif block == 0x0E:
            hardware.incubator = bool(data[0] & 0x01)
        elif block == 0x1F:
            hardware.spectrometer = bool(data[0] & 0x01)
        elif block == 0x2F:
            text, _, rest = data.partition(b"\x00")
            hardware.filter_slides = text.decode("ascii", "replace")
            hardware.monochromators = bool(rest and rest[0] & 0x01)


PARAMETER_OFFSET = 7  # stored parameter at address n is at payload byte n + 7
ONE_BYTE_PARAMETERS = (128, 132, 133, 134, 139, 141)
TWO_BYTE_PARAMETERS = (154, 156, 158, 170, 206, 208)


def decode_parameters(payload: bytes) -> dict[int, int]:
    """The stored parameters a driver uses, by address (little-endian, unsigned)."""
    values: dict[int, int] = {}
    for address in ONE_BYTE_PARAMETERS:
        if len(payload) > address + PARAMETER_OFFSET:
            values[address] = payload[address + PARAMETER_OFFSET]
    for address in TWO_BYTE_PARAMETERS:
        if len(payload) >= address + PARAMETER_OFFSET + 2:
            values[address] = struct.unpack_from("<H", payload, address + PARAMETER_OFFSET)[0]
    return values


def decode_firmware_info(payload: bytes) -> tuple[int, str]:
    """(version x1000, "Nov 20 2020 11:51:21")."""
    version = int.from_bytes(payload[6:8], "big")
    texts = [t.decode("ascii", "replace") for t in payload[8:].split(b"\x00") if t]
    return version, " ".join(texts[:2])


# -- RUN encoding -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PlateGeometry:
    """Plate layout as the reader wants it: mm, x from the left edge, y from the BACK (row A) edge."""

    size_x_mm: float
    size_y_mm: float
    a1_x_mm: float
    a1_y_mm: float
    last_x_mm: float
    last_y_mm: float
    columns: int
    rows: int

    def encode(self) -> bytes:
        hundredths = (round(v * 100) for v in (self.size_x_mm, self.size_y_mm, self.a1_x_mm, self.a1_y_mm, self.last_x_mm, self.last_y_mm))
        return struct.pack(">6H2B", *hundredths, self.columns, self.rows)


def well_mask(columns: int, cells: Iterable[tuple[int, int]]) -> bytes:
    """48-byte mask; bit ``row * columns + column`` (0-based), most significant bit first."""
    mask = bytearray(MAX_WELLS // 8)
    for row, column in cells:
        bit = row * columns + column
        if not 0 <= bit < MAX_WELLS:
            raise ValueError(f"well ({row}, {column}) does not fit the {MAX_WELLS}-well mask")
        mask[bit // 8] |= 0x80 >> (bit % 8)
    return bytes(mask)


def settling_byte(seconds: float) -> int:
    return max(round(seconds * 50), 1)


def u16_tenths(nm: float) -> bytes:
    return struct.pack(">H", round(nm * 10))


def run_start(geometry: PlateGeometry, cells: Iterable[tuple[int, int]], *, mode: int, bottom: bool = False, edr: bool = False,
              scan: int = SCAN_DEFAULT, settling_s: float = 0.0) -> bytes:
    """Family byte, plate block (63), scan byte, pre-separator (31), separator, settling byte: a point read."""
    pre = bytearray(31)
    pre[0] = mode | (BOTTOM_OPTIC if bottom else 0)
    pre[1] = EDR if edr else 0
    plate = geometry.encode() + b"\x00" + well_mask(geometry.columns, cells)
    return bytes([RUN]) + plate + bytes([scan]) + bytes(pre) + SEPARATOR + bytes([settling_byte(settling_s)])


def run_end(trailer: int, flashes: int) -> bytes:
    """Cycles 1, ``flashes`` (luminescence: the measurement interval), cycle time 1 s."""
    return bytes([trailer]) + bytes.fromhex("000000000001000000") + b"\x01" + struct.pack(">HH", flashes, 1) + b"\x00"


WATER_PATH = bytes.fromhex("232826ca00000064")  # always sent; meaning unknown
PAUSE_OFF = bytes.fromhex("00000005")


def absorbance_run(start: bytes, wavelengths_nm: list[int], *, flashes: int, spectrum: tuple[int, int, int] | None = None) -> bytes:
    """Discrete (1-8 wavelengths) or, with ``spectrum=(start, end, step)`` nm, a spectrum scan."""
    if spectrum is not None:
        first, last, step = spectrum
        body = b"\x00" + u16_tenths(first) + u16_tenths(last) + u16_tenths(step)
    else:
        if not 1 <= len(wavelengths_nm) <= 8:
            raise ValueError("a discrete absorbance read takes 1-8 wavelengths")
        body = bytes([len(wavelengths_nm)]) + b"".join(u16_tenths(nm) for nm in wavelengths_nm) + b"\x00\x00" + u16_tenths(10)
    return start + body + WATER_PATH + PAUSE_OFF + run_end(0x02, flashes)


def band(center_nm: float, width_nm: float) -> tuple[int, int]:
    """Monochromator band as (upper, lower) edges in 0.1 nm."""
    return round((center_nm + width_nm / 2) * 10), round((center_nm - width_nm / 2) * 10)


def dichroic_split(excitation: tuple[int, int], emission: tuple[int, int]) -> int:
    """Midpoint of the facing band edges, 0.1 nm, rounded half to even."""
    if excitation[0] < emission[0]:
        return round((excitation[0] + emission[1]) / 2)
    return round((emission[0] + excitation[1]) / 2)


EX_MONOCHROMATOR, EM_MONOCHROMATOR = 0x03, 0x04


def channel_block(gain: int, excitation: tuple[int, int], emission: tuple[int, int], dichroic: int | None = None) -> bytes:
    """One fluorescence channel through both monochromators (17 bytes)."""
    split = dichroic_split(excitation, emission) if dichroic is None else dichroic
    return struct.pack(">6H", gain, *excitation, split, *emission) + bytes([0, EM_MONOCHROMATOR, 0, EX_MONOCHROMATOR, 0])


def fluorescence_run(start: bytes, channels: list[bytes], *, focal_height_mm: float, flashes: int) -> bytes:
    if not 1 <= len(channels) <= 5:
        raise ValueError("a fluorescence read takes 1-5 channels")
    head = struct.pack(">HBH", round(focal_height_mm * 100), 0x00, len(channels)) + bytes(5) + b"\x0c"
    return start + head + b"\x00\x00\x0c".join(channels) + bytes(3) + run_end(0x00, flashes)


def luminescence_run(start: bytes, *, focal_height_mm: float, gain: int, emission: tuple[int, int], normalization_s: float,
                     interval_s: float) -> bytes:
    """Top-optic point read through the emission monochromator (``start`` built with mode luminescence, EDR on)."""
    body = (struct.pack(">H", round(focal_height_mm * 100)) + bytes.fromhex("0000010000") + struct.pack(">H", gain)
            + bytes.fromhex("000100010001") + struct.pack(">HH", *emission) + bytes.fromhex("00050000")
            + struct.pack(">H", round(normalization_s * 100)) + bytes(4))
    return start + body + run_end(0x00, round(interval_s * 100))


# -- data replies ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DataPage:
    layout: int
    count: int
    """Values in this page."""
    wells: int
    temperature_c: float | None
    values: tuple[int, ...]
    ceiling: int | None = None


_VALUE_START = {LAYOUT_ABSORBANCE: 36, LAYOUT_FLUORESCENCE: 34, LAYOUT_LUMINESCENCE: 34}
_TEMPERATURE_AT = {LAYOUT_ABSORBANCE: 34, LAYOUT_FLUORESCENCE: 32, LAYOUT_LUMINESCENCE: 32}
_WELLS_AT = {LAYOUT_ABSORBANCE: 20, LAYOUT_FLUORESCENCE: 18, LAYOUT_LUMINESCENCE: 18}


def decode_data(payload: bytes) -> DataPage:
    """One REQUEST DATA reply (type 0x02): its layout, value count, well count, temperature and u32 values."""
    if len(payload) < 7 or payload[0] != REPLY_DATA:
        raise FrameError(f"not a data reply (type 0x{payload[0]:02X})" if payload else "empty data reply")
    layout = payload[6]
    if layout not in _VALUE_START:
        raise FrameError(f"unknown data layout 0x{layout:02X}")
    start = _VALUE_START[layout]
    if len(payload) < start:
        raise FrameError("data reply header is truncated")
    count = int.from_bytes(payload[7:9], "big")
    if len(payload) != start + 4 * count:
        raise FrameError(f"data reply announces {count} values but carries {(len(payload) - start) / 4:g}")
    temp = int.from_bytes(payload[_TEMPERATURE_AT[layout]:_TEMPERATURE_AT[layout] + 2], "big")
    wells = int.from_bytes(payload[_WELLS_AT[layout]:_WELLS_AT[layout] + 2], "big")
    ceiling = None if layout == LAYOUT_ABSORBANCE else int.from_bytes(payload[11:15], "big")
    return DataPage(layout, count, wells, temp / 10 if temp else None, struct.unpack_from(f">{count}I", payload, start), ceiling)


def absorbance_values_expected(wavelengths: int, wells: int) -> int:
    return (wavelengths + 3) * (wells + 2)


def optical_densities(values: list[int] | tuple[int, ...], wavelengths: int, wells: int) -> list[list[float]]:
    """OD[wavelength][well] from an absorbance reply's values (all pages joined).

    Layout: ``n + 3`` groups of ``wells`` counts (samples per wavelength, two undecoded groups, the
    reference), then ``n + 3`` calibration pairs in the same order.
    ``T = (sample / c) * (r / reference)``, ``OD = -log10(T)``; c and r are the first values of the
    wavelength's and of the reference's calibration pair.
    """
    n, w = wavelengths, wells
    if len(values) != absorbance_values_expected(n, w):
        raise FrameError(f"{len(values)} absorbance values; expected {absorbance_values_expected(n, w)}")
    calibration = values[(n + 3) * w:]
    reference, r = values[(n + 2) * w:(n + 3) * w], calibration[2 * (n + 2)]
    result: list[list[float]] = []
    for k in range(n):
        c = calibration[2 * k]
        samples = values[k * w:(k + 1) * w]
        result.append([_od(sample, c, ref, r) for sample, ref in zip(samples, reference)])
    return result


def _od(sample: int, c: int, reference: int, r: int) -> float:
    if sample <= 0 or reference <= 0 or c <= 0 or r <= 0:
        return math.inf
    return -math.log10((sample / c) * (r / reference))
