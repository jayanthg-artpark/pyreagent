"""Byonoy HID report framing: 64-byte reports, u16 report id first, two routing bytes last."""

from __future__ import annotations

import struct
from dataclasses import dataclass

VENDOR_ID = 0x16D0
PRODUCT_IDS = {"absorbance_96": 0x1199, "luminescence_96": 0x119B}
REPORT_SIZE = 64

# Report ids (Byonoy's own command ids, not USB HID report ids).
SUPPORTED_REPORTS = 0x0010
ACK = 0x0020
ABORT = 0x0060
DATA_READ = 0x0200
STATUS = 0x0300
ENVIRONMENT = 0x0310
ABS_TRIGGER = 0x0320
WAVELENGTHS = 0x0330
LUM_TRIGGER = 0x0340
ABS_RESULT = 0x0500
LUM_RESULT = 0x0600

# Routing bytes (62, 63): from the PC and answer me / from the PC, no answer / fire-and-forget.
REPLY = b"\x80\x40"
NO_REPLY = b"\x00\x40"
LEGACY = b"\x00\x00"


def frame(report: int, payload: bytes = b"", routing: bytes = REPLY) -> bytes:
    if len(payload) > REPORT_SIZE - 4:
        raise ValueError(f"payload of {len(payload)} bytes does not fit a report")
    return (struct.pack("<H", report) + payload).ljust(REPORT_SIZE - 2, b"\x00") + routing


def report_id(data: bytes) -> int:
    return struct.unpack_from("<H", data)[0]


@dataclass(frozen=True)
class Status:
    initialized: bool
    slot: str
    """"empty", "occupied", "undetermined" or "unknown" (the plate sensor is not fully reliable)."""
    error_code: int
    uptime_s: int
    measuring: bool
    booted: bool

    @classmethod
    def decode(cls, data: bytes) -> "Status":
        initialized, slot, error, uptime, measuring, booted = struct.unpack_from("<BBBIBB", data, 2)
        return cls(bool(initialized), {0: "empty", 1: "occupied", 2: "undetermined"}.get(slot, "unknown"), error, uptime,
                   bool(measuring), bool(booted))


def read_field(data: bytes) -> int | str | bool | float | bytes:
    """Value of a device-data field reply (report 0x0200): the low nibble of byte 4 is its type."""
    kind, value = data[4] & 0x0F, data[5:57]
    if kind == 1:
        return struct.unpack_from("<I", value)[0]
    if kind == 2:
        return value.split(b"\x00", 1)[0].decode("utf-8", "replace")
    if kind == 3:
        return value[0] != 0
    if kind == 4:
        return struct.unpack_from("<f", value)[0]
    return bytes(value)
