"""A fake Tecan Infinite 200 PRO on the USB bulk transport: real framing, enough state for the driver.

It checks every host frame (length and checksum), answers with device frames built the same way,
streams one binary packet per well on SCANX (values from ``signal(x, y)`` at the stage position),
and sends the calibration packet where a test asks for it.
"""

from __future__ import annotations

import asyncio
import struct
from collections import deque
from typing import Any, Callable, Literal

from pyreagent.devices.plate_reader.tecan.protocol import encode

ESC_FRAME = b"\x1b" + bytes(30)


def binary(payload: bytes) -> bytes:
    return encode(f"{len(payload)},BIN:") + payload + b"\x00\x00\x00\x00"


class FakeInfinite:
    def __init__(
        self,
        *,
        calibration_at: Literal["prepare", "scan", "end"] | None = "prepare",
        busy_with_st: bool = False,
        chunk_size: int | None = None,
        status_frames: bool = False,
    ) -> None:
        self.calibration_at = calibration_at
        self.busy_with_st = busy_with_st
        """Send ``BY#T<ms>`` and its ``ST`` in one USB transfer."""
        self.chunk_size = chunk_size
        """Split measurement streams into transfers of this size (default: one per packet)."""
        self.status_frames = status_frames
        """Put a 31-byte ESC status frame before every well packet."""
        self.signal: Callable[[int, int], int] = lambda x, y: 500
        self.position = "HOME"
        self.temperature = {"CURRENT": "218", "TARGET": "0", "STATUS": "OFF"}
        self.shaking = {"MODE": "ORBITAL", "AMPLITUDE": "-1", "TIME": "-1"}
        self.beam_diameter = "700|1000:2"
        self.fail: dict[str, list[str]] = {}
        """Command -> reply frames to send once instead (in one transfer); [] means no reply."""
        self.commands: list[str] = []
        self.reads: list[tuple[str | None, float | None]] = []
        """(last command, timeout) for every read the driver makes."""
        self.is_open = False
        self._pending: deque[bytes] = deque()
        self._mode = ""
        self._labels: dict[str, int] = {"ex": 0, "em": 0}
        self._y = 0
        self._calibrated = False

    # -- transport ----------------------------------------------------------------------------------

    async def open(self) -> None:
        self.is_open = True

    async def close(self) -> None:
        self.is_open = False

    async def write(self, data: bytes) -> None:
        if not self.is_open:
            raise ConnectionError("fake Infinite is closed")
        await asyncio.sleep(0)  # like real I/O, give other tasks a turn
        text = self.decode(data)
        self.commands.append(text)
        self._pending.extend(self._respond(text))

    async def read(self, timeout_s: float | None = None) -> bytes:
        self.reads.append((self.commands[-1] if self.commands else None, timeout_s))
        if not self._pending:
            await asyncio.sleep(0)
            raise TimeoutError("no data")
        return self._pending.popleft()

    def describe(self) -> dict[str, Any]:
        return {"transport": "fake-infinite"}

    @staticmethod
    def decode(frame: bytes) -> str:
        end = frame.index(0x03)
        text = frame[1:end]
        checksum = 0x01
        for byte in text:
            checksum ^= byte
        assert frame[0] == 0x02 and frame[end + 1:] == bytes([0, 0, len(text) & 0xFF, checksum, 0x0D]), frame
        return text.decode("ascii")

    # -- instrument ---------------------------------------------------------------------------------

    def queries(self) -> list[str]:
        return [c for c in self.commands if c[0] in "?"]

    def _busy(self, ms: int) -> list[bytes]:
        if self.busy_with_st:
            return [encode(f"BY#T{ms}") + encode("ST")]
        return [encode(f"BY#T{ms}"), encode("ST")]

    def _calibration(self) -> bytes:
        self._calibrated = True
        if self._mode == "ABS":
            return binary(struct.pack(">HHIHHHHHHH", 0, self._labels["ex"], 0, 0, 0, 0, 1000, 0, 0, 1000))
        if self._mode == "FI.TOP":
            return binary(struct.pack(">H8xH2xHH", self._labels["ex"], 0, 0, 1000))
        return binary(bytes(6) + struct.pack(">i", 0))

    def _packet(self, signal: int) -> bytes:
        if self._mode == "ABS":
            return binary(struct.pack(">HH6xHH", 0, self._labels["ex"], signal, 1000))
        if self._mode == "FI.TOP":
            return binary(struct.pack(">HHH6xHH", 0, self._labels["ex"], self._labels["em"], signal, 1000))
        return binary(struct.pack(">HH6xi", 0, 0, signal))

    def _scan(self, x_start: int, x_end: int, count: int) -> list[bytes]:
        items = []
        for i in range(count):
            x = x_start + (x_end - x_start) * i // max(1, count - 1)
            if self.status_frames:
                items.append(ESC_FRAME)
            items.append(self._packet(self.signal(x, self._y)))
            if i == 0 and self.calibration_at == "scan" and not self._calibrated:
                items.append(self._calibration())
        items.append(encode("ST"))
        if self.chunk_size is None:
            return items
        stream = b"".join(items)
        return [stream[i:i + self.chunk_size] for i in range(0, len(stream), self.chunk_size)]

    def _respond(self, text: str) -> list[bytes]:
        if text in self.fail:
            frames = self.fail.pop(text)
            return [b"".join(encode(f) for f in frames)] if frames else []
        if text[0] in "?#":
            return [encode(self._query(text))]
        keyword, _, args = text.partition(" ")
        if keyword == "INIT":
            self.position = "IN"
            return self._busy(80000)
        if keyword == "MODE":
            self._mode = args
            return self._busy(5000)
        if text.endswith(" CLEAR"):
            return [encode("+")]
        if keyword in ("EXCITATION", "EMISSION") and args[0] == "0":
            self._labels["ex" if keyword == "EXCITATION" else "em"] = int(args.split(",")[2])
        if text in ("ABSOLUTE MTP,OUT", "ABSOLUTE MTP,IN"):
            self.position = args.split(",")[1]
            return self._busy(5000)
        if keyword == "ABSOLUTE":
            self._y = int(args.split("Y=")[1])
        if keyword == "TEMPERATURE" or (keyword == "SHAKING" and "=" in args):
            key, value = args.removeprefix("PLATE,").split("=")
            (self.temperature if keyword == "TEMPERATURE" else self.shaking)[key] = value
        if text == "SHAKING ON":
            return self._busy(int(self.shaking["TIME"]) * 1000)
        if keyword == "KEYLOCK" and args == "ON":
            self._calibrated = False
        if keyword == "PREPARE":
            return [encode("ST"), *([self._calibration()] if self.calibration_at == "prepare" else [])]
        if keyword == "SCANX":
            return self._scan(*(int(v) for v in args.split(",")))
        if keyword == "TERMINATE" and self.calibration_at == "end" and not self._calibrated:
            return [encode("ST"), self._calibration()]
        return [encode("ST")]

    def _query(self, text: str) -> str:
        if text == "?ABSOLUTE MTP,POS":
            return self.position
        if text.startswith("?TEMPERATURE PLATE,"):
            return self.temperature[text.split(",")[1]]
        if text.startswith("?SHAKING "):
            return self.shaking[text.split(" ")[1]]
        if text == "?SENSOR PLATEPOS":
            return "TAKEN"
        if text == "#BEAM DIAMETER":
            return self.beam_diameter
        return "0"
