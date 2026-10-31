"""Raw TCP socket transport (Ethernet-to-serial servers, instruments with a TCP command port)."""

from __future__ import annotations

import asyncio
from typing import Any

from .transport import Transport


class TcpTransport(Transport):
    def __init__(self, host: str, port: int, *, connect_timeout_s: float = 5.0) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.connect_timeout_s = connect_timeout_s
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def open(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self.connect_timeout_s
        )

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except OSError:
                pass
        self._reader = self._writer = None

    async def write(self, data: bytes) -> None:
        if self._writer is None:
            raise ConnectionError(f"not connected to {self.host}:{self.port}")
        self._writer.write(data)
        await self._writer.drain()

    async def read_until(self, terminator: bytes, timeout_s: float) -> bytes:
        if self._reader is None:
            raise ConnectionError(f"not connected to {self.host}:{self.port}")
        try:
            return await asyncio.wait_for(self._reader.readuntil(terminator), timeout=timeout_s)
        except asyncio.IncompleteReadError as exc:
            raise ConnectionError(f"{self.host}:{self.port} closed the connection") from exc

    async def read_exactly(self, size: int, timeout_s: float) -> bytes:
        if self._reader is None:
            raise ConnectionError(f"not connected to {self.host}:{self.port}")
        try:
            return await asyncio.wait_for(self._reader.readexactly(size), timeout=timeout_s)
        except asyncio.IncompleteReadError as exc:
            raise ConnectionError(f"{self.host}:{self.port} closed the connection") from exc

    async def discard_input(self) -> None:
        reader = self._reader
        if reader is None:
            return
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=0.001)
            except TimeoutError:
                return
            if not chunk:
                return

    def describe(self) -> dict[str, Any]:
        return {"transport": "tcp", "host": self.host, "port": self.port}
