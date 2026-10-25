"""Hamilton STAR / STARlet firmware text protocol over USB.

Protocol facts from PyLabRobot's reverse-engineered STAR backend (MIT), re-implemented:

    USB VID 0x08AF, PID 0x8000 (STAR/STARlet; the Vantage is 0x8003). Bulk endpoints, no framing:
    one USB transfer is one ASCII command or reply, with no terminator.
    request   "<module(2)><command(2)>id<NNNN><key(2)><value>..."   values fixed-width, zero-padded
    reply     "<module><command>id<NNNN>er<code>/<trace>[ <module><code>/<trace>...]<key><value>..."

The id (1..9999, wrapping) pairs a reply with its request. Per-channel values are space separated,
one per channel up to the highest channel used, with unused positions holding a copy of the first
value; when that list is shorter than the head, one more padding value and a trailing "&" follow.
Units: 0.1 mm, 0.1 µL, 0.1 µL/s, 0.1 s. Modules addressed directly (``P1``..``PG`` channels,
``H0`` 96 head, ``R0`` iSWAP) reply ``er<trace>`` only.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Mapping, Protocol, Sequence

from ...core.errors import CommunicationError, DeviceTimeout, HardwareError, InvalidArgument, NeedsAttention, PyreagentError

log = logging.getLogger("pyreagent.hamilton")

VENDOR_ID = 0x08AF
STAR_PRODUCT_ID = 0x8000

CHANNEL_MODULES = "123456789ABCDEFG"

MASTER_ERRORS = {
    1: "command syntax error", 2: "hardware error", 3: "command not completed", 4: "clot detected", 5: "barcode unreadable",
    6: "too little liquid", 7: "tip already fitted", 8: "no tip", 9: "no carrier", 10: "execution not completed",
    11: "dispense with pressure LLD", 12: "no teach-in signal", 13: "loading tray error", 15: "parameter combination not allowed",
    16: "cover open", 17: "aspiration error", 18: "wash fluid or waste error", 19: "incubation error", 20: "TADM measurement error",
    21: "no element", 22: "element still holding", 23: "element lost", 24: "illegal target plate position", 25: "illegal user access",
    26: "TADM measurement error", 27: "position not reachable", 28: "unexpected LLD", 29: "area already occupied",
    30: "impossible to occupy area", 31: "anti-drop control error", 99: "subsystem error",
}
MASTER_TRACES = {
    10: "CAN error", 11: "subsystem command time out", 30: "unknown command", 31: "unknown parameter", 32: "parameter out of range",
    33: "parameter does not belong to command, or not all parameters were sent", 35: "id parameter error", 40: "PIP task busy",
    45: "iSWAP task busy", 46: "CoRe 96 head task busy",
}
CHANNEL_TRACES = {
    30: "unknown command", 31: "unknown parameter", 32: "parameter out of range", 36: "stop during execution of command",
    50: "dispensing drive init position not found", 51: "dispensing drive not initialized", 52: "dispensing drive movement error",
    53: "maximum volume in tip reached", 54: "position outside of permitted area", 55: "Y drive blocked", 57: "Y drive movement error",
    60: "Z drive blocked", 62: "Z drive movement error", 65: "squeezer drive blocked", 70: "no liquid level found",
    71: "not enough liquid present", 73: "no liquid level found with dual LLD", 74: "liquid at a not allowed position detected",
    75: "no tip picked up", 76: "tip already picked up", 77: "tip not dropped", 78: "wrong tip picked up",
    80: "liquid not correctly aspirated", 81: "clot detected", 82: "TADM measurement out of lower limit curve",
    83: "TADM measurement out of upper limit curve", 87: "2nd phase of liquid not found", 88: "not enough liquid present",
}
NO_LIQUID = {70, 71, 73, 87, 88}
SYNTAX = {30, 31, 32, 33, 35}

_ERRORS = re.compile(r"er(\d{2})(?:/(\d{2}))?((?: ?[A-Z][0-9A-Z]\d{2}/\d{2})*)")
_MODULE_ERROR = re.compile(r"([A-Z][0-9A-Z])(\d{2})/(\d{2})")
_FIELD = re.compile(r"([a-z]{2})([^a-z]*)")


def channel_module(channel: int) -> str:
    """Module name of a pipetting channel (0 = the back-most): P1..P9, PA..PG."""
    return "P" + CHANNEL_MODULES[channel]


def _describe(module: str, code: int, trace: int) -> str:
    if module == "C0":
        text = MASTER_ERRORS.get(code, f"error {code}")
        return f"{text} ({MASTER_TRACES[trace]})" if trace in MASTER_TRACES else text
    if module.startswith("P"):
        return CHANNEL_TRACES.get(trace, f"trace {trace}")
    return f"error {code}, trace {trace}" if code else f"trace {trace}"


def module_errors(reply: str) -> dict[str, tuple[int, int]]:
    """Failing modules in a reply: {module: (code, trace)}; empty when the reply reports success."""
    match = _ERRORS.search(reply)
    if match is None:
        return {}
    module = reply[:2]
    errors: dict[str, tuple[int, int]] = {}
    if match.group(2) is None:  # a module addressed directly: er<trace>
        trace = int(match.group(1))
        if trace:
            errors[module] = (0, trace)
        return errors
    code, trace = int(match.group(1)), int(match.group(2))
    for sub, sub_code, sub_trace in _MODULE_ERROR.findall(match.group(3)):
        if (int(sub_code), int(sub_trace)) != (0, 0):
            errors[sub] = (int(sub_code), int(sub_trace))
    if (code, trace) != (0, 0) and not (code == 99 and errors):
        errors[module] = (code, trace)
    return errors


def error_for(errors: Mapping[str, tuple[int, int]], command: str) -> PyreagentError:
    """One structured pyreagent error for the failing modules of a reply."""
    parts = [f"{m} {_describe(m, c, t)}" for m, (c, t) in errors.items()]
    message = f"STAR {command[:4]} failed: " + "; ".join(parts) + "."
    details = {"command": command[:4], "errors": {m: {"code": c, "trace": t} for m, (c, t) in errors.items()}}
    codes = {c for c, _ in errors.values()}
    traces = {t for _, t in errors.values()}
    if traces & NO_LIQUID or 6 in codes:
        return NeedsAttention(message, suggested_action="Check the source wells have enough liquid, then retry or ask a human.",
                              details=details)
    if 81 in traces or 4 in codes:
        return NeedsAttention(message, suggested_action="A clot was detected; inspect the tip and the sample.", details=details)
    if traces & {75, 76, 77, 78} or codes & {7, 8}:
        return NeedsAttention(message, suggested_action="The tips on the channels differ from what pyreagent expected; ask a "
                              "human to check the channels and the tip rack, then reset the device.", details=details)
    if 27 in codes or 54 in traces:
        return InvalidArgument(message, suggested_action="The target is outside the reachable area; check the carrier rail "
                               "and labware.", details=details)
    if 16 in codes:
        return NeedsAttention(message, suggested_action="Close the front cover, then retry.", details=details)
    if 1 in codes or traces & SYNTAX:
        return CommunicationError(message, recoverable=False, details=details)
    return HardwareError(message, details=details)


def fields(reply: str) -> dict[str, str]:
    """``{key: raw value}`` of a reply's data fields (after module, command, id and error)."""
    body = _ERRORS.sub("", reply[4:], count=1)
    return {key: value.strip() for key, value in _FIELD.findall(body)}


def ints(value: str) -> list[int]:
    return [int(v) for v in value.split()]


def pad(value: float, width: int) -> str:
    return f"{round(value):0{width}d}"


def channel_list(values: Mapping[int, str], channels: int, fill: str | None = None) -> list[str]:
    """Positional per-channel list (see module docstring); ``values`` maps channel -> formatted value.

    Unused positions hold ``fill`` (positions and the tip pattern use zeros) or else the first value.
    """
    used = sorted(values)
    length = used[-1] + 1
    if length < channels:
        length += 1
    filler = values[used[0]] if fill is None else fill
    return [values.get(ch, filler) for ch in range(length)]


Value = str | int | Sequence[str]


def encode(module: str, command: str, id_: int, params: Sequence[tuple[str, Value]], channels: int) -> str:
    text = f"{module}{command}id{id_:04d}"
    for key, value in params:
        if isinstance(value, (list, tuple)):
            value = " ".join(value) + ("&" if len(value) < channels else "")
        text += f"{key}{value}"
    return text


class PacketTransport(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def write(self, data: bytes) -> None: ...
    async def read(self, timeout_s: float | None = None) -> bytes: ...
    def describe(self) -> dict[str, Any]: ...


class StarFirmware:
    """Serialised request/reply over the USB link, with id matching and error mapping."""

    def __init__(self, transport: PacketTransport, *, timeout_s: float = 30.0) -> None:
        self.transport = transport
        self.timeout_s = timeout_s
        self.channels = 8
        self._id = 0
        self._lock = asyncio.Lock()

    def _next_id(self) -> int:
        self._id = self._id % 9999 + 1
        return self._id

    async def send(self, module: str, command: str, params: Sequence[tuple[str, Value]] = (), *,
                   timeout_s: float | None = None) -> dict[str, str]:
        async with self._lock:
            id_ = self._next_id()
            text = encode(module, command, id_, params, self.channels)
            await self.transport.write(text.encode("ascii"))
            reply = await self._reply(module + command, id_, timeout_s or self.timeout_s)
        errors = module_errors(reply)
        if errors:
            raise error_for(errors, text)
        return fields(reply)

    async def _reply(self, prefix: str, id_: int, timeout_s: float) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        expected = f"{prefix}id{id_:04d}"
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise DeviceTimeout(f"No reply from the STAR to {prefix} within {timeout_s:g} s.")
            try:
                reply = (await self.transport.read(remaining)).decode("ascii", "replace").strip("\x00\r\n ")
            except TimeoutError:
                continue
            if reply.startswith(expected):
                return reply
            if reply:
                log.debug("STAR: ignoring unrelated reply %r while waiting for %s", reply, expected)
