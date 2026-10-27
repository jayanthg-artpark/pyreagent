"""USB HID transport (hidapi, run in a worker thread).

For instruments that talk in fixed-size HID reports (Byonoy readers). Moves one
report at a time; framing and reply matching belong to the driver. Needs the
optional ``hidapi`` package. Same shape as ``UsbBulkTransport``, so drivers and
tests (``MockUsbTransport``) treat both alike.
"""

from __future__ import annotations

import asyncio
from typing import Any


class HidTransport:
    def __init__(
        self,
        vendor_id: int,
        product_id: int,
        *,
        serial_number: str | None = None,
        report_size: int = 64,
        report_id: int = 0,
        poll_s: float = 0.05,
    ) -> None:
        self.vendor_id, self.product_id = vendor_id, product_id
        self.serial_number = serial_number
        self.report_size = report_size
        self.report_id = report_id
        self.poll_s = poll_s
        self._device: Any = None

    async def open(self) -> None:
        try:
            import hid
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError("HID instruments need hidapi: pip install 'pyreagent[hid]'") from exc

        def find() -> Any:
            matches = [d for d in hid.enumerate(self.vendor_id, self.product_id)
                       if self.serial_number is None or d.get("serial_number") == self.serial_number]
            serials = sorted({d.get("serial_number") or "" for d in matches})
            if not matches:
                raise ConnectionError(f"no HID device {self.vendor_id:04x}:{self.product_id:04x}"
                                      + (f" with serial {self.serial_number}" if self.serial_number else ""))
            if len(serials) > 1:
                raise ConnectionError(f"{len(serials)} matching HID devices ({', '.join(serials)}); set serial_number")
            device = hid.device()
            device.open_path(matches[0]["path"])
            return device

        self._device = await asyncio.to_thread(find)

    async def close(self) -> None:
        if self._device is not None:
            await asyncio.to_thread(self._device.close)
            self._device = None

    def _open_device(self) -> Any:
        if self._device is None:
            raise ConnectionError("HID device is not open")
        return self._device

    async def write(self, data: bytes) -> None:
        if len(data) != self.report_size:
            raise ValueError(f"HID reports are {self.report_size} bytes, got {len(data)}")
        written = await asyncio.to_thread(self._open_device().write, bytes([self.report_id]) + data)
        if written < 0:
            raise ConnectionError("HID write failed (device unplugged?)")

    async def read(self, timeout_s: float | None = None) -> bytes:
        """One report. Raises ``TimeoutError`` if none arrives in time."""
        device = self._open_device()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (timeout_s if timeout_s is not None else 10.0)
        while True:  # short blocking reads, so a cancelled task is noticed between them
            data = await asyncio.to_thread(device.read, self.report_size, int(self.poll_s * 1000))
            if data:
                return bytes(data)
            if loop.time() > deadline:
                raise TimeoutError("no report from the HID device")

    def describe(self) -> dict[str, Any]:
        return {"transport": "hid", "vendor_id": f"{self.vendor_id:04x}", "product_id": f"{self.product_id:04x}",
                "serial_number": self.serial_number}
