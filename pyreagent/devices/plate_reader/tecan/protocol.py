"""Tecan Infinite 200 PRO wire format: ASCII frames, binary data packets, and their conversion to OD/RFU/RLU.

Both directions use ``STX text ETX 00 00 len xor CR``; ``len`` is the text length mod 256 and
``xor`` the XOR of the text bytes, then XOR 0x01. Device frames are not checked (their trailer
was never confirmed). Binary data follows a text frame ``<N>,BIN:`` as N raw bytes plus a
4-byte trailer, with no framing.
"""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass
from statistics import fmean
from typing import Literal

VENDOR_ID = 0x0C47
PRODUCT_ID = 0x8007

STX, ETX, CR, ESC = 0x02, 0x03, 0x0D, 0x1B
STATUS_FRAME_SIZE = 31  # ESC-led status frames seen during scans; content unknown, skipped whole
TERMINAL = frozenset({"ST", "+", "-"})

_BIN_HEADER = re.compile(r"(\d+),BIN:")
_BUSY = re.compile(r"\+?BY#T(\d+)")
_BUSY_STATUS = re.compile(r"BY(#.\d+|%\d+|\$.*)?")


class CalibrationError(ValueError):
    """The calibration and data packets cannot produce a value (zero denominators)."""


def encode(text: str) -> bytes:
    payload = text.encode("ascii")
    checksum = 0x01
    for byte in payload:
        checksum ^= byte
    return bytes([STX]) + payload + bytes([ETX, 0, 0, len(payload) & 0xFF, checksum, CR])


def busy_ms(text: str) -> int | None:
    """Milliseconds the device announced in a timed-busy frame (``BY#T5000``, ``+BY#T0``), else None."""
    match = _BUSY.fullmatch(text)
    return int(match.group(1)) if match else None


def is_terminal(text: str) -> bool:
    return text in TERMINAL or text.startswith("ERR")


def decode_status(text: str) -> str:
    """Meaning of a ``QQ`` reply: standby, power_down, power_up, parked, busy, busy_in_background, message or unknown."""
    fixed = {"ST": "standby", "PD": "power_down", "PU": "power_up", "PA": "parked"}
    if text in fixed:
        return fixed[text]
    if _BUSY_STATUS.fullmatch(text.removeprefix("+")):
        return "busy_in_background" if text.startswith("+") else "busy"
    return "message" if text.startswith("MSG") else "unknown"


def parse_capability(text: str, default: int) -> int:
    """Leading number of a ``#...`` capability reply (text before any ``|``, ``:`` or ``~``)."""
    try:
        return int(float(text.split("|")[0].split(":")[0].split("~")[0].strip()))
    except ValueError:
        return default


class FrameParser:
    """Splits the device's byte stream into text frames (``str``) and binary payloads (``bytes``)."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._binary: int | None = None

    def reset(self) -> None:
        self._buffer.clear()
        self._binary = None

    @property
    def binary_pending(self) -> bool:
        return self._binary is not None

    def feed(self, data: bytes, *, bare_ascii: bool = False) -> list[str | bytes]:
        """Add ``data`` and return every complete item. ``bare_ascii``: take an unframed printable
        buffer as one text frame (a defensive fallback for command replies)."""
        buf = self._buffer
        buf += data
        out: list[str | bytes] = []
        while buf:
            if self._binary is not None:
                if len(buf) < self._binary + 4:
                    break
                out.append(bytes(buf[: self._binary]))
                del buf[: self._binary + 4]  # payload, then a 4-byte trailer of unknown meaning
                self._binary = None
            elif buf[0] == STX:
                end = buf.find(ETX, 1)
                if end < 0 or len(buf) < end + 5:
                    break
                text = buf[1:end].decode("ascii", "replace")
                header = _BIN_HEADER.fullmatch(text)
                if header and len(buf) == end + 5:
                    break  # wait: the next byte is either this frame's CR or the first payload byte
                del buf[: end + (6 if len(buf) > end + 5 and buf[end + 5] == CR else 5)]
                if header:
                    self._binary = int(header.group(1))
                else:
                    out.append(text)
            elif buf[0] == ESC:
                if len(buf) < STATUS_FRAME_SIZE:
                    break
                del buf[:STATUS_FRAME_SIZE]
            elif bare_ascii and all(0x20 <= b <= 0x7E for b in buf):
                out.append(buf.decode("ascii"))
                buf.clear()
            else:
                del buf[0]  # a late CR or noise between frames
        return out


# -- measurement packets ----------------------------------------------------------------------------

PacketKind = Literal["calibration", "data", "unknown"]
TMIN = 1e-4  # transmission floor: OD is at most 4


@dataclass(frozen=True)
class AbsorbanceReference:
    meas_dark: int
    meas_bright: int
    ref_dark: int
    ref_bright: int


@dataclass(frozen=True)
class FluorescenceReference:
    meas_dark: int
    ref_dark: int
    ref_bright: int


def _items(payload: bytes, offset: int, fmt: str) -> list[tuple[int, ...]]:
    """10-byte items from ``offset``: 6 unknown bytes, then ``fmt``."""
    return [struct.unpack_from(fmt, payload, at + 6) for at in range(offset, len(payload), 10)]


def optical_density(references: list[AbsorbanceReference], items: list[tuple[int, int]]) -> float:
    """OD from (sample, reference) detector counts and the run's calibration items."""
    if len(references) == len(items) > 1:  # one calibration item per read
        transmissions = []
        for c, (meas, ref) in zip(references, items):
            if c.meas_bright == c.meas_dark or ref == c.ref_dark:
                continue
            factor = (c.ref_bright - c.ref_dark) / (c.meas_bright - c.meas_dark)
            transmissions.append((meas - c.meas_dark) / (ref - c.ref_dark) * factor)
        if not transmissions:
            raise CalibrationError("every read had a zero calibration denominator")
        return -math.log10(max(fmean(transmissions), TMIN))
    c = references[0]
    if c.meas_bright == c.meas_dark:
        raise CalibrationError("sample detector bright and dark counts are equal")
    factor = (c.ref_bright - c.ref_dark) / (c.meas_bright - c.meas_dark)
    transmissions = [(meas - c.meas_dark) / (ref - c.ref_dark) for meas, ref in items if ref != c.ref_dark]
    if not transmissions:
        raise CalibrationError("no read had reference counts above dark")
    return -math.log10(max(fmean(transmissions) * factor, TMIN))


def fluorescence_rfu(reference: FluorescenceReference | None, items: list[tuple[int, int]]) -> float:
    if not items:
        return 0.0
    meas, ref = fmean(m for m, _ in items), fmean(r for _, r in items)
    if reference is None:
        return float(round(meas))
    if ref == reference.ref_dark:
        return 0.0
    return float(round((meas - reference.meas_dark) * (reference.ref_bright - reference.ref_dark) / (ref - reference.ref_dark)))


def luminescence_rlu(dark_counts: int | None, counts: list[int], dark_s: float, integration_s: float) -> float:
    """Dark-corrected count rate (counts/s, truncated); the plain mean count without calibration."""
    if not counts:
        return 0.0
    mean = fmean(counts)
    if dark_counts is None or dark_s == 0 or integration_s == 0:
        return float(round(mean))
    return float(int(mean / integration_s - dark_counts / dark_s))


class AbsorbanceDecoder:
    """Buffers a run's packets; ``results`` converts them once the run is over."""

    def __init__(self) -> None:
        self.references: list[AbsorbanceReference] | None = None
        self.wells: list[list[tuple[int, int]]] = []

    @property
    def calibrated(self) -> bool:
        return self.references is not None

    def add(self, payload: bytes) -> PacketKind:
        n = len(payload)
        # Checked first: lengths such as 94 also fit the data rule.
        if n >= 22 and (n - 4) % 18 == 0:
            if self.references is None:  # only the first calibration packet of a run counts
                # item: u32 ticker overflows, u16 ticker, u16 gain, dark, bright, u16 ref gain, ref dark, ref bright
                self.references = [AbsorbanceReference(*struct.unpack_from(">8xHH2xHH", payload, at))
                                   for at in range(4, n, 18)]
            return "calibration"
        if n >= 14 and (n - 4) % 10 == 0:
            self.wells.append([(meas, ref) for meas, ref in _items(payload, 4, ">HH")])
            return "data"
        return "unknown"

    def results(self) -> list[float]:
        assert self.references is not None
        return [optical_density(self.references, items) for items in self.wells]


class FluorescenceDecoder:
    def __init__(self) -> None:
        self.reference: FluorescenceReference | None = None
        self.wells: list[list[tuple[int, int]]] = []

    @property
    def calibrated(self) -> bool:
        return self.reference is not None

    def add(self, payload: bytes) -> PacketKind:
        n = len(payload)
        if n == 18:  # a later calibration packet replaces an earlier one
            meas_dark, ref_dark, ref_bright = struct.unpack_from(">H2xHH", payload, 10)
            self.reference = FluorescenceReference(meas_dark, ref_dark, ref_bright)
            return "calibration"
        if n >= 16 and (n - 6) % 10 == 0:
            self.wells.append([(meas, ref) for meas, ref in _items(payload, 6, ">HH")])
            return "data"
        return "unknown"

    def results(self) -> list[float]:
        return [fluorescence_rfu(self.reference, items) for items in self.wells]


class LuminescenceDecoder:
    def __init__(self, *, dark_s: float, integration_s: float) -> None:
        self.dark_s, self.integration_s = dark_s, integration_s
        self.dark_counts: int | None = None
        self.wells: list[list[int]] = []

    @property
    def calibrated(self) -> bool:
        return self.dark_counts is not None

    def add(self, payload: bytes) -> PacketKind:
        n = len(payload)
        if n == 10:
            self.dark_counts = struct.unpack_from(">i", payload, 6)[0]
            return "calibration"
        if n >= 14 and (n - 4) % 10 == 0:
            self.wells.append([count for (count,) in _items(payload, 4, ">i")])
            return "data"
        return "unknown"

    def results(self) -> list[float]:
        return [luminescence_rlu(self.dark_counts, counts, self.dark_s, self.integration_s) for counts in self.wells]


Decoder = AbsorbanceDecoder | FluorescenceDecoder | LuminescenceDecoder
