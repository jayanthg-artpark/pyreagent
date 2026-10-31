"""BioTek serial protocol: framing, status words, checksums, measurement records and result frames.

Two families share the command/ACK/status framing: RDOP22 (Synergy H1, Cytation 1/5), which
defines a read with one ASCII ``D`` record, and the legacy Synergy HT protocol, which sets a
read up with binary parameter commands. Every checksum here is a byte sum mod 256.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ....core.errors import CommunicationError
from ....labware import LabwareGeometry

ACK, NAK, ETX, SUB, CR = b"\x06", b"\x15", b"\x03", b"\x1a", b"\r"
POWER_ON_FLAG = 0x1E
"""Synergy HT: first character of every status word until ``n 1`` is sent."""

Rect = tuple[int, int, int, int]
"""(first row, first column, last row, last column), 1-based and inclusive."""

# -- opaque Gen5 constants -------------------------------------------------------------------------
# Copied from captured Gen5 traffic and serializer vectors; their meaning is NOT understood
# (they presumably hold flashes, gain, read speed and optics). Change nothing here without a capture.
RECORD_STEP = "01"
NORMAL_PLATE_MODE = "0001200100001"
READ_EVENT = "1001000001"
ABS_FLASHES = "00008"
FLUOR_HEAD = "35000100200200"
FLUOR_TAIL = "21001"
LUM_HEAD = "2300"
LUM_MID = "200200-001000-003"
LUM_TAIL = "01351"
ZEROS_18 = "0" * 18
SHAKE_FIXED = "00002000000013"
HT_FOCUS = b"40000" + ETX
"""Synergy HT ``t`` parameter sent before every read."""

FOCUS_OFFSET_UM = 14220
"""``t`` encodes the read height as a Z position in µm, offset by 14.22 mm."""
FOCUS_FLUORESCENCE, FOCUS_LUMINESCENCE = "6", "3"
HT_PRESETS = {(4, 6): 0x00, (8, 12): 0x02}
"""Synergy HT built-in plate layouts for ``{``."""


def checksum(data: bytes | str) -> int:
    raw = data.encode("ascii") if isinstance(data, str) else data
    return sum(raw) & 0xFF


# -- status words ----------------------------------------------------------------------------------

def split_status(reply: bytes, *, power_on_flag: bool = False) -> tuple[bytes, int]:
    """Split ``<data><SSSS><ETX>`` into data and the status code (0 = success).

    ``power_on_flag``: accept the Synergy HT's 0x1E in place of the first status digit.
    """
    tail = bytearray(reply[-5:])
    if power_on_flag and len(tail) == 5 and tail[0] == POWER_ON_FLAG:
        tail[0] = ord("0")
    if not re.fullmatch(rb"[0-9A-Fa-f]{4}\x03", bytes(tail)):
        raise CommunicationError(f"Malformed status in reader reply {reply!r}.")
    return reply[:-5], int(tail[:4], 16)


def parse_identity(data: bytes) -> tuple[str, str]:
    """``e`` reply data -> (basecode, version)."""
    match = re.fullmatch(rb"\s*(\d+)\s+Version\s+(\S+)\s*", data)
    if match is None:
        raise CommunicationError(f"Unrecognised identity {data!r}.")
    return match.group(1).decode(), match.group(2).decode()


def parse_capability(data: bytes) -> tuple[bool, str]:
    """``z`` reply data (17 bytes) -> (supported, the 16 detail characters)."""
    if len(data) != 17 or data[:1] not in (b"0", b"1"):
        raise CommunicationError(f"Malformed capability reply {data!r}.")
    return data[:1] == b"1", data[1:].decode("ascii", "replace")


def parse_temperature(digits: bytes) -> float | None:
    """Three digits of tenths of °C; ``111``/``999`` mean the sensor is unavailable."""
    if not re.fullmatch(rb"\d{3}", digits):
        raise CommunicationError(f"Malformed temperature {digits!r}.")
    return None if digits in (b"111", b"999") else int(digits) / 10


# -- RDOP22 records --------------------------------------------------------------------------------

def record(body: str, *, etx: bool = False) -> bytes:
    """``D`` parameter: 4-digit length (body + checksum), body, 3-digit checksum of the body."""
    return f"{len(body) + 3:04d}{body}{checksum(body):03d}".encode("ascii") + (ETX if etx else b"")


def _prefix(rect: Rect) -> str:
    r0, c0, r1, c1 = rect
    return f"{RECORD_STEP}{r0:02d}{c0:02d}{r1:02d}{c1:02d}{NORMAL_PLATE_MODE}{READ_EVENT}"


def absorbance_body(rect: Rect, wavelength_nm: int, *, extra_field: bool, extended_optics: bool) -> str:
    """``extra_field``: capability 2056; ``extended_optics``: capability 2064."""
    return f"{_prefix(rect)}0{'6' if extended_optics else ''}{ABS_FLASHES}{'000' if extra_field else ''}{wavelength_nm:04d}"


def fluorescence_body(rect: Rect, excitation_nm: int, emission_nm: int) -> str:
    return f"{_prefix(rect)}{FLUOR_HEAD}{excitation_nm:04d}000{emission_nm:04d}{ZEROS_18}{FLUOR_TAIL}"


def luminescence_body(rect: Rect, integration_s: float) -> str:
    """Integration time in 0.2 s steps below 20 s: ``int(T)*5`` and ``frac(T)*50``, 2 digits each (Gen5's encoding)."""
    tenths = round(integration_s * 10)
    whole, fraction = divmod(tenths, 10)
    if whole * 5 > 99:
        raise ValueError("integration time must be below 20 s")
    return f"{_prefix(rect)}{LUM_HEAD}{whole * 5:02d}{fraction * 5:02d}{LUM_MID}{ZEROS_18}{LUM_TAIL}"


def shake_body(duration_s: int, *, orbital: bool, amplitude_mm: int) -> str:
    return f"{RECORD_STEP}01010101{SHAKE_FIXED}{duration_s:03d}{int(orbital)}{amplitude_mm}0"


def parse_record_reply(data: bytes) -> str:
    """``D`` reply without its status: ``LLL`` + data + ``CCC`` (LLL counts everything after itself, status included)."""
    text = data.decode("ascii", "replace")
    if len(text) < 6 or not text[:3].isdigit() or int(text[:3]) != len(text) - 3 + 5:
        raise CommunicationError(f"Bad length in assay-definition reply {data!r}.")
    body, check = text[3:-3], text[-3:]
    if not check.isdigit() or int(check) != checksum(body):
        raise CommunicationError(f"Bad checksum in assay-definition reply {data!r}.")
    return body


def focus(mode: str, height_mm: float) -> bytes:
    return f"{mode}{FOCUS_OFFSET_UM + round(height_mm * 1000):05d}".encode("ascii") + ETX


def geometry(rows: int, columns: int, g: LabwareGeometry) -> bytes:
    """``y`` parameter: well centres measured from the plate's back-left corner, plate size and height, in 0.01 mm.

    pyreagent geometry has its origin at the front-left with y toward the back, so the
    distance from the back edge is ``size_y - y``. The height is the plate's (no lid).
    """
    def cmm(mm: float, width: int) -> str:
        return f"{round(mm * 100):0{width}d}"
    first_x, first_y, _ = g.well_center(0, 0)
    last_x, last_y, _ = g.well_center(rows - 1, columns - 1)
    fields = (f"{rows:02d}{columns:02d}", cmm(g.size_y_mm - first_y, 5), cmm(g.size_y_mm - last_y, 5), cmm(first_x, 5),
              cmm(last_x, 5), cmm(g.size_y_mm, 5), cmm(g.size_x_mm, 5), cmm(g.size_z_mm, 4))
    text = "".join(fields)
    if len(text) != 38:
        raise ValueError(f"plate geometry does not fit the reader's fields: {text}")
    return text.encode("ascii") + ETX


@dataclass
class Result:
    values: dict[tuple[int, int], float]
    """(row, column), 1-based -> value; NaN for overflow."""
    temperature_c: float | None


def parse_result(frame: bytes, *, verify_checksum: bool = True) -> Result:
    """RDOP22 result frame (status already removed): ``01,1,<CR>`` time, temp, ``RR,CC,value`` triplets, temp, checksum."""
    if not frame.startswith(b"01,1,\r") or frame.count(SUB) != 2 or not frame.endswith(SUB):
        raise CommunicationError(f"Malformed result frame {frame[:40]!r}.")
    body, check, _ = frame[6:].split(SUB)
    if verify_checksum and (not check.isdigit() or int(check) != checksum(body + SUB)):
        raise CommunicationError(f"Result frame checksum mismatch ({check!r}).")
    readings, sep, temp = body.rpartition(b"\r\n")
    fields = readings.replace(b"\r\n", b"").decode("ascii", "replace").split(",")
    if not sep or len(fields) < 2 or (len(fields) - 2) % 3:
        raise CommunicationError(f"Malformed result frame {frame[:40]!r}.")
    values: dict[tuple[int, int], float] = {}
    for i in range(2, len(fields), 3):
        row, column, value = fields[i : i + 3]
        try:
            values[(int(row), int(column))] = math.nan if "*" in value else float(value)
        except ValueError:
            raise CommunicationError(f"Malformed reading {row},{column},{value!r} in result frame.") from None
    return Result(values, parse_temperature(temp))


# -- Synergy HT ------------------------------------------------------------------------------------

def ht_setup_packet(wavelength_nm: int, *, shake_s: int = 0, shake_intensity: int = 0) -> bytes:
    """170-byte ``V`` packet: byte 0 is the checksum of bytes 1..169.

    Shake intensity: 0 slow, 1 medium, 2 fast, 3 variable (pre-read shaking).
    """
    packet = bytearray(170)
    packet[8] = ord("0")
    packet[29] = 0x10
    packet[49:52] = b"000"
    packet[66:84] = f"{wavelength_nm:03d}".encode() + b"000" * 5
    if shake_s > 0:
        packet[30] = 0x10
        packet[162:164] = shake_s.to_bytes(2, "little")
        packet[164] = shake_intensity
    packet[0] = checksum(bytes(packet[1:]))
    return bytes(packet)


def ht_frame_length(rows: int, columns: int) -> int:
    return 23 + rows * (6 * columns + 2)


def parse_ht_result(frame: bytes, rows: int, columns: int) -> tuple[list[list[float]], float | None]:
    """Synergy HT result frame (fixed length, status included) -> rows of OD values, temperature."""
    if len(frame) != ht_frame_length(rows, columns) or frame[:1] != CR:
        raise CommunicationError(f"Malformed result frame {frame[:40]!r}.")
    if not re.fullmatch(rb"\d{3}:[0-5]\d:[0-5]\d\.\d", frame[1:12]):
        raise CommunicationError(f"Malformed timestamp in result frame {frame[1:12]!r}.")
    end = 12 + rows * (6 * columns + 2)
    sub = end + 3
    if frame[sub : sub + 1] != SUB or frame[sub + 2 : sub + 3] != SUB:
        raise CommunicationError("Malformed result frame trailer.")
    if frame[sub + 1] != checksum(frame[1 : sub + 1]):
        raise CommunicationError("Result frame checksum mismatch.")
    table: list[list[float]] = []
    for r in range(rows):
        line = frame[12 + r * (6 * columns + 2) : 12 + (r + 1) * (6 * columns + 2)]
        if not line.endswith(b"\r\n"):
            raise CommunicationError(f"Malformed result row {line!r}.")
        row: list[float] = []
        for c in range(columns):
            field = line[6 * c : 6 * c + 6]
            if field.startswith(b",*"):
                row.append(math.nan)
            elif re.fullmatch(rb",[+-]\d{4}", field):
                row.append(int(field[1:]) / 1000)
            else:
                raise CommunicationError(f"Malformed reading {field!r} in result frame.")
        table.append(row)
    return table, parse_temperature(frame[end:sub])


# -- well selection --------------------------------------------------------------------------------

def cover(cells: set[tuple[int, int]]) -> list[Rect]:
    """Cover (row, column) cells exactly with rectangles: from the first cell left, extend right, then down."""
    left = set(cells)
    rects: list[Rect] = []
    while left:
        r0, c0 = min(left)
        c1 = c0
        while (r0, c1 + 1) in left:
            c1 += 1
        r1 = r0
        while all((r1 + 1, c) in left for c in range(c0, c1 + 1)):
            r1 += 1
        left -= {(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)}
        rects.append((r0, c0, r1, c1))
    return sorted(rects)


def bounding(cells: set[tuple[int, int]]) -> Rect:
    rows, columns = [r for r, _ in cells], [c for _, c in cells]
    return min(rows), min(columns), max(rows), max(columns)
