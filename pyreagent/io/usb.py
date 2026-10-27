"""USB bulk-endpoint transport (pyusb, run in a worker thread), plus a scripted mock for tests.

Used by robots whose firmware talks over raw USB bulk endpoints (Hamilton STAR,
Tecan EVO). The transport moves byte blocks; command framing and reply
matching belong to the driver. Needs the optional ``pyusb`` package and a
libusb-compatible driver for the device (on Windows, typically installed with Zadig).
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Callable


class UsbBulkTransport:
    def __init__(
        self,
        vendor_id: int,
        product_id: int,
        *,
        serial_number: str | None = None,
        interface: int = 0,
        read_size: int = 64,
        timeout_s: float = 10.0,
        zero_length_packets: bool = False,
    ) -> None:
        """``zero_length_packets``: end a write whose length is a multiple of the endpoint's packet
        size with an empty packet, for firmware that waits for a short packet to end a transfer."""
        self.vendor_id, self.product_id = vendor_id, product_id
        self.serial_number = serial_number
        self.interface = interface
        self.read_size = read_size
        self.timeout_s = timeout_s
        self.zero_length_packets = zero_length_packets
        self._device: Any = None
        self._out: Any = None
        self._in: Any = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        try:
            import usb.core
            import usb.util
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError("USB robots need pyusb: pip install 'pyreagent[usb]'") from exc

        def find() -> Any:
            matches = list(usb.core.find(find_all=True, idVendor=self.vendor_id, idProduct=self.product_id))
            if self.serial_number is not None:
                matches = [d for d in matches if usb.util.get_string(d, d.iSerialNumber) == self.serial_number]
            if not matches:
                raise ConnectionError(f"no USB device {self.vendor_id:04x}:{self.product_id:04x}"
                                      + (f" with serial {self.serial_number}" if self.serial_number else ""))
            if len(matches) > 1:
                raise ConnectionError(f"{len(matches)} matching USB devices; set serial_number")
            device = matches[0]
            device.set_configuration()
            intf = device.get_active_configuration()[(self.interface, 0)]
            direction = usb.util.endpoint_direction
            out = usb.util.find_descriptor(intf, custom_match=lambda e: direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
            inn = usb.util.find_descriptor(intf, custom_match=lambda e: direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
            return device, out, inn

        self._device, self._out, self._in = await asyncio.to_thread(find)

    async def close(self) -> None:
        if self._device is not None:
            import usb.util

            await asyncio.to_thread(usb.util.dispose_resources, self._device)
            self._device = self._out = self._in = None

    async def write(self, data: bytes) -> None:
        if self._out is None:
            raise ConnectionError("USB device is not open")
        await asyncio.to_thread(self._out.write, data, int(self.timeout_s * 1000))
        if self.zero_length_packets and data and len(data) % self._out.wMaxPacketSize == 0:
            await asyncio.to_thread(self._out.write, b"", int(self.timeout_s * 1000))

    async def read(self, timeout_s: float | None = None) -> bytes:
        """One bulk read (a USB transfer ends on a short packet). Raises ``TimeoutError`` if nothing arrives."""
        if self._in is None:
            raise ConnectionError("USB device is not open")
        import usb.core

        try:
            data = await asyncio.to_thread(self._in.read, self.read_size, int((timeout_s or self.timeout_s) * 1000))
        except usb.core.USBTimeoutError as exc:
            raise TimeoutError("no reply from the USB device") from exc
        return bytes(data)

    def describe(self) -> dict[str, Any]:
        return {"transport": "usb", "vendor_id": f"{self.vendor_id:04x}", "product_id": f"{self.product_id:04x}",
                "serial_number": self.serial_number}


class MockUsbTransport:
    """Plays an instrument: ``responder`` receives each written block and returns the reply blocks."""

    def __init__(self, responder: Callable[[bytes], bytes | list[bytes] | None]) -> None:
        self._respond = responder
        self.sent: list[bytes] = []
        self.is_open = False
        self._pending: deque[bytes] = deque()

    async def open(self) -> None:
        self.is_open = True

    async def close(self) -> None:
        self.is_open = False

    async def write(self, data: bytes) -> None:
        if not self.is_open:
            raise ConnectionError("mock USB device is closed")
        self.sent.append(data)
        reply = self._respond(data)
        if isinstance(reply, bytes):
            self._pending.append(reply)
        elif reply:
            self._pending.extend(reply)

    async def read(self, timeout_s: float | None = None) -> bytes:
        if not self._pending:
            raise TimeoutError("no scripted reply")
        return self._pending.popleft()

    def describe(self) -> dict[str, Any]:
        return {"transport": "mock-usb"}
