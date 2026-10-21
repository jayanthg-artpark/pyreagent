"""Scripted transport for testing real drivers without hardware.

Give it a responder that plays the instrument: it receives each written
command and returns the bytes the instrument would send back.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .transport import Transport


class MockTransport(Transport):
    def __init__(self, responder: Callable[[bytes], bytes | None] | Mapping[bytes, bytes]) -> None:
        super().__init__()
        if isinstance(responder, Mapping):
            table = dict(responder)
            self._respond: Callable[[bytes], bytes | None] = table.get
        else:
            self._respond = responder
        self.sent: list[bytes] = []
        self.is_open = False
        self._rx = bytearray()

    async def open(self) -> None:
        self.is_open = True

    async def close(self) -> None:
        self.is_open = False

    async def write(self, data: bytes) -> None:
        if not self.is_open:
            raise ConnectionError("mock transport is closed")
        self.sent.append(data)
        reply = self._respond(data)
        if reply:
            self._rx += reply

    async def read_until(self, terminator: bytes, timeout_s: float) -> bytes:
        index = self._rx.find(terminator)
        if index < 0:
            raise TimeoutError(f"no scripted reply (pending {bytes(self._rx)!r})")
        end = index + len(terminator)
        data = bytes(self._rx[:end])
        del self._rx[:end]
        return data

    async def discard_input(self) -> None:
        self._rx.clear()

    def describe(self) -> dict[str, Any]:
        return {"transport": "mock"}
