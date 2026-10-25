"""Tecan Freedom EVO firmware protocol over USB.

Protocol facts from PyLabRobot's reverse-engineered EVO backend (MIT), re-implemented:

    USB VID 0x0C47, PID 0x4000 (bulk endpoints; on Windows install libusbK for the "TECU" device with Zadig)
    request   STX(0x02) + "<module><MNEMONIC><p1>,<p2>,..." + 0x00     no checksum, no length
    reply     STX + module(2) + status byte + "<d1>,<d2>,..." + 0x00   error code = status ^ 0x80 (0 = ok)

Modules: C5 = LiHa (pipetting arm), C1 = RoMa (plate gripper), W1 = MCA (96 head; init/park only).
Parameters are integers (1/10 mm, 1/10 degree, plunger full steps, plunger speed in half-steps/s);
an empty field leaves that channel or parameter unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, Sequence

from ...core.errors import CommunicationError, HardwareError, NeedsAttention, PyreagentError

VENDOR_ID = 0x0C47
PRODUCT_ID = 0x4000

LIHA, ROMA, MCA = "C5", "C1", "W1"

ERRORS: dict[str, dict[int, str]] = {
    LIHA: {1: "initialization failed", 2: "invalid command", 3: "invalid operand", 4: "CAN acknowledge problem",
           5: "device not implemented", 6: "CAN answer timeout", 7: "not initialized", 8: "command overflow of TeCU",
           9: "no liquid detected", 10: "drive no load", 11: "not enough liquid", 12: "not enough liquid", 13: "no Flash access",
           15: "command overflow of subdevice", 17: "measurement failed", 18: "clot limit passed", 19: "no clot exit detected",
           20: "no liquid exit detected", 21: "delta pressure overrun (pLLD)", 22: "tip guard in wrong position",
           23: "not yet moved or move aborted", 24: "LLD pulse or reed crosstalk", 25: "tip not fetched", 26: "tip not mounted",
           27: "tip mounted", 28: "subdevice error", 29: "application switch and axes mismatch", 30: "wrong DC-servo type",
           31: "virtual drive"},
    ROMA: {1: "initialization failed", 2: "invalid command", 3: "invalid operand", 4: "CAN acknowledge problem",
           5: "device not implemented", 6: "CAN answer timeout", 7: "not initialized", 8: "command overflow of TeCU",
           9: "plate not fetched", 10: "drive no load", 11: "subdevice not ready", 13: "no Flash-EPROM", 14: "hardware not defined",
           15: "command overflow of subdevice", 17: "verification failed", 21: "BCS communication error", 25: "download error",
           28: "subdevice error", 30: "invalid servo version"},
}
NOT_PRESENT = 5


def error_for(module: str, code: int, command: str) -> PyreagentError:
    """Structured pyreagent error for a firmware error code."""
    meaning = ERRORS.get(module, {}).get(code, f"unknown error {code}")
    details = {"module": module, "code": code, "command": command}
    message = f"EVO {module} error {code}: {meaning}."
    if code == 7:
        return NeedsAttention(message, recoverable=True, suggested_action="Call home to initialise the arm, then retry.", details=details)
    if module == LIHA and code in (9, 11, 12, 18, 20):
        return NeedsAttention(message, suggested_action="Check the source well has enough liquid and no clots, then retry or ask a human.",
                              details=details)
    if code in (2, 3):
        return CommunicationError(message, recoverable=False, details=details)
    return HardwareError(message, details=details)


class PacketTransport(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def write(self, data: bytes) -> None: ...
    async def read(self, timeout_s: float | None = None) -> bytes: ...
    def describe(self) -> dict[str, Any]: ...


def encode(module: str, command: str, params: Sequence[int | None] = ()) -> bytes:
    body = module + command + ",".join("" if p is None else str(int(p)) for p in params)
    return b"\x02" + body.encode("ascii") + b"\x00"


def decode(reply: bytes) -> tuple[str, int, list[int | str]]:
    """(module, error code, data fields) of one reply frame."""
    if len(reply) < 4:
        raise CommunicationError(f"Short EVO reply {reply!r}.")
    module = reply[1:3].decode("ascii", "replace")
    code = reply[3] ^ 0x80 if reply[3] & 0x80 else reply[3]
    data: list[int | str] = []
    for field in reply[4:].rstrip(b"\x00").decode("ascii", "replace").split(","):
        if field == "":
            continue
        try:
            data.append(int(field))
        except ValueError:
            data.append(field)
    return module, code, data


class EvoFirmware:
    """Serialised request/reply over the USB link, with error mapping and the set-command cache."""

    def __init__(self, transport: PacketTransport, *, timeout_s: float = 60.0) -> None:
        self.transport = transport
        self.timeout_s = timeout_s
        self.sent: list[str] = []
        self._last_set: dict[tuple[str, str], tuple[int | None, ...]] = {}
        self._lock = asyncio.Lock()

    async def send(self, module: str, command: str, params: Sequence[int | None] = (), *, timeout_s: float | None = None) -> list[int | str]:
        params = tuple(params)
        # Set-commands ("S..") change parameters; skip resending identical values, as EVOware does.
        if command.startswith("S") and self._last_set.get((module, command)) == params:
            return []
        frame = encode(module, command, params)
        async with self._lock:
            await self.transport.write(frame)
            reply = await self.transport.read(timeout_s or self.timeout_s)
        self.sent.append(frame[1:-1].decode("ascii"))
        _, code, data = decode(reply)
        if code:
            self._last_set.pop((module, command), None)
            raise error_for(module, code, frame[1:-1].decode("ascii"))
        if command.startswith("S"):
            self._last_set[(module, command)] = params
        return data

    def forget(self) -> None:
        """Clear the set-command cache (after re-initialisation the instrument forgets them too)."""
        self._last_set.clear()
