"""Tecan's ASCII instrument framing, shared by the Infinite readers and the HydroFlex washer.

Each frame is ``STX text ETX 00 00 len xor CR``: ``len`` is the text length mod 256 and ``xor``
the XOR of the text bytes, then XOR 0x01. Actions answer ``BY#T<ms>`` (busy, with how long the
move will take) and then ``ST`` (standby); errors are ``ERR<n>:<text>``.
"""

from __future__ import annotations

import re

STX, ETX, CR = 0x02, 0x03, 0x0D

_BUSY = re.compile(r"\+?BY#T(\d+)")


class FrameError(ValueError):
    """A frame's trailer does not match its text."""


def checksum(payload: bytes) -> int:
    value = 0x01
    for byte in payload:
        value ^= byte
    return value


def encode(text: str) -> bytes:
    payload = text.encode("ascii")
    return bytes([STX]) + payload + bytes([ETX, 0, 0, len(payload) & 0xFF, checksum(payload), CR])


def busy_ms(text: str) -> int | None:
    """Milliseconds the device announced in a timed-busy frame (``BY#T5000``, ``+BY#T0``), else None."""
    match = _BUSY.fullmatch(text)
    return int(match.group(1)) if match else None


def take_frame(buffer: bytearray) -> str | None:
    """Remove the first complete frame from ``buffer`` and return its text (None: not complete yet).

    Bytes before the first STX are dropped. Raises ``FrameError`` when the trailer is wrong.
    """
    start = buffer.find(STX)
    if start < 0:
        buffer.clear()
        return None
    del buffer[:start]
    end = buffer.find(ETX, 1)
    if end < 0 or len(buffer) < end + 6:
        return None
    payload, trailer = bytes(buffer[1:end]), bytes(buffer[end + 1:end + 6])
    del buffer[:end + 6]
    if trailer != bytes([0, 0, len(payload) & 0xFF, checksum(payload), CR]):
        raise FrameError(f"frame {payload!r} has trailer {trailer.hex(' ')}")
    return payload.decode("ascii", "replace")
