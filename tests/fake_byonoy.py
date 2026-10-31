"""A fake Byonoy reader for the HID transport: real 64-byte framing, enough behaviour for the driver."""

from __future__ import annotations

import struct

from pyreagent.devices.plate_reader.byonoy.protocol import (
    ABS_RESULT, ABS_TRIGGER, DATA_READ, ENVIRONMENT, LUM_RESULT, LUM_TRIGGER, REPLY, STATUS, WAVELENGTHS, frame,
)

ABS_OD = [round(0.05 + i * 0.01, 2) for i in range(96)]  # A1, A2, ... H12
LUM_RLU = [i * 10.0 - 5.0 for i in range(96)]  # dark wells can read slightly negative


class FakeByonoy:
    def __init__(self, model: str = "absorbance_96", *, wavelengths: tuple[int, ...] = (600, 450), planes: int = 1) -> None:
        self.model = model
        self.wavelengths = wavelengths
        self.planes = planes
        self.error_code = 0
        self.error_after_read: int | None = None
        """Error code the status reports once the next measurement finishes."""
        self.hang = False
        """Accept triggers but never send results (to test cancel and timeouts)."""
        self.temperature_c = 23.5
        self.sent: list[bytes] = []
        self.triggers: list[bytes] = []

    def __call__(self, data: bytes) -> list[bytes]:
        assert len(data) == 64, len(data)
        self.sent.append(data)
        report, routing = struct.unpack_from("<H", data)[0], data[62:]
        if report in (ABS_TRIGGER, LUM_TRIGGER):
            return self._measure(report, data)
        if routing != REPLY:  # fire-and-forget: the prelude writes and abort
            return []
        if report == STATUS:
            return [frame(STATUS, struct.pack("<BBBIBB", 1, 1, self.error_code, 232, 0, 1), b"\x00\x40")]
        if report == DATA_READ:
            index = struct.unpack_from("<H", data, 2)[0]
            text = {1: "Absorbance 96" if self.model == "absorbance_96" else "Luminescence 96", 3: "BYO-1234", 4: "2024-10-23"}
            return [frame(DATA_READ, struct.pack("<HB", index, 0xC2) + text.get(index, "").encode() + b"\x00", b"\x00\x00")]
        if report == WAVELENGTHS:
            return [frame(WAVELENGTHS, struct.pack(f"<{len(self.wavelengths)}h", *self.wavelengths))]
        if report == ENVIRONMENT:
            return [frame(ENVIRONMENT, struct.pack("<hhhhh", round(self.temperature_c * 100), 450, 0, 0, 16384))]
        return []

    def _measure(self, trigger: int, data: bytes) -> list[bytes]:
        self.triggers.append(data)
        if self.hang:
            return []
        if self.error_after_read is not None:
            self.error_code, self.error_after_read = self.error_after_read, None
        if trigger == ABS_TRIGGER:
            wavelength = struct.unpack_from("<h", data, 2)[0]
            head, result, values = struct.pack("<hhI", wavelength, 0, 1500), ABS_RESULT, ABS_OD
        else:
            head, result, values = struct.pack("<II", struct.unpack_from("<i", data, 2)[0], 2500), LUM_RESULT, LUM_RLU * self.planes
        count = len(values) // 12
        chunks = [frame(result, struct.pack("<BB", seq, count) + head + struct.pack("<12f", *values[seq * 12:seq * 12 + 12])
                        + bytes([0, round(100 * (seq + 1) / count)]), b"\x00\x40") for seq in range(count)]
        stray = frame(0x0020, struct.pack("<HH", 0x0040, 0), b"\x00\x40")  # an unrelated ack the driver must skip
        return [chunks[1], stray, chunks[0], *chunks[2:]]  # slightly out of order, like a real USB queue can be
