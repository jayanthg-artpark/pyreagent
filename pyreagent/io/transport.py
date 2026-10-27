"""Byte transports that drivers sit on.

A ``Transport`` moves bytes; it does not know any vendor protocol. Drivers
call ``query`` for request/response exchanges, which holds a lock so a status
poll and a command never interleave on the wire.
"""

from __future__ import annotations

import abc
import asyncio
from typing import Any


class Transport(abc.ABC):
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @abc.abstractmethod
    async def open(self) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...

    @abc.abstractmethod
    async def write(self, data: bytes) -> None: ...

    @abc.abstractmethod
    async def read_until(self, terminator: bytes, timeout_s: float) -> bytes:
        """Read up to and including ``terminator``. Raise ``TimeoutError`` if it does not arrive."""

    async def read_exactly(self, size: int, timeout_s: float) -> bytes:
        """Read exactly ``size`` bytes, whatever they contain (binary replies). Raise ``TimeoutError`` if they do not arrive."""
        raise NotImplementedError(f"{type(self).__name__} cannot read fixed-length replies")

    async def discard_input(self) -> None:
        """Drop unread bytes (e.g. a late reply to a command that timed out)."""

    async def query(self, data: bytes, *, terminator: bytes, timeout_s: float) -> bytes:
        async with self._lock:
            await self.discard_input()
            await self.write(data)
            return await self.read_until(terminator, timeout_s)

    def describe(self) -> dict[str, Any]:
        return {"transport": type(self).__name__}
