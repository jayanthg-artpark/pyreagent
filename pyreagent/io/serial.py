"""RS-232 / USB-serial transport (pyserial, run in a worker thread)."""

from __future__ import annotations

import asyncio
from typing import Any

from .transport import Transport


class SerialTransport(Transport):
    def __init__(
        self,
        port: str,
        *,
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: float = 1,
        rtscts: bool = False,
        xonxoff: bool = False,
    ) -> None:
        super().__init__()
        self.port = port
        self.settings = dict(baudrate=baudrate, bytesize=bytesize, parity=parity, stopbits=stopbits, rtscts=rtscts, xonxoff=xonxoff)
        self._serial: Any = None

    async def open(self) -> None:
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - depends on install
            raise ImportError("Serial drivers need pyserial: pip install 'pyreagent[serial]'") from exc
        self._serial = await asyncio.to_thread(serial.Serial, port=self.port, timeout=1, write_timeout=2, **self.settings)

    async def close(self) -> None:
        if self._serial is not None:
            await asyncio.to_thread(self._serial.close)
            self._serial = None

    def _port(self) -> Any:
        if self._serial is None:
            raise ConnectionError(f"serial port {self.port} is not open")
        return self._serial

    async def write(self, data: bytes) -> None:
        port = self._port()
        await asyncio.to_thread(port.write, data)
        await asyncio.to_thread(port.flush)

    async def read_until(self, terminator: bytes, timeout_s: float) -> bytes:
        port = self._port()
        port.timeout = timeout_s
        data: bytes = await asyncio.to_thread(port.read_until, terminator)
        if not data.endswith(terminator):
            raise TimeoutError(f"no reply on {self.port} within {timeout_s:g}s (got {data!r})")
        return data

    async def read_exactly(self, size: int, timeout_s: float) -> bytes:
        port = self._port()
        port.timeout = timeout_s
        data: bytes = await asyncio.to_thread(port.read, size)
        if len(data) < size:
            raise TimeoutError(f"{len(data)} of {size} bytes from {self.port} within {timeout_s:g}s (got {data!r})")
        return data

    async def discard_input(self) -> None:
        await asyncio.to_thread(self._port().reset_input_buffer)

    def describe(self) -> dict[str, Any]:
        return {"transport": "serial", "port": self.port, "baudrate": self.settings["baudrate"]}
