"""Mettler Toledo balances over MT-SICS (Mettler Toledo Standard Interface Command Set).

Implemented from the MT-SICS reference: ASCII commands terminated by CR LF,
replies of the form ``<command> <status> [<value> <unit>]``. Uses only level 0
and level 1 commands, which every MT-SICS balance supports:

    @    cancel all pending commands; reply ``I4 A "<serial number>"``
    I2   model and capacity; reply ``I2 A "<type> <capacity> <unit>"``
    S    stable weight;      ``S S <value> <unit>``
    SI   immediate weight;   ``S S|D <value> <unit>`` (D = dynamic / not settled)
    T    tare (stable);      ``T S <value> <unit>``
    Z    zero (stable);      ``Z A``

Status ``I`` means not executable (busy, or not settled within the balance's
own stability timeout); ``+`` / ``-`` mean over / under range. Lines starting
``ES``, ``ET``, ``EL`` are syntax, transmission and logical errors.

Wiring: the balance interface must be set to MT-SICS (not "print" / "host
continuous"), with matching baud rate, 8 data bits, no parity, 1 stop bit.
"""

from __future__ import annotations

import re
from typing import Any

from ....core.errors import CommunicationError, DeviceTimeout, NeedsAttention, NotSupported
from ....io import Transport, line_transport
from ..backend import BalanceBackend
from ..models import Weight

_TOKEN = re.compile(r'"([^"]*)"|(\S+)')
_TO_GRAMS = {"g": 1.0, "mg": 1e-3, "ug": 1e-6, "µg": 1e-6, "kg": 1e3}


def tokenize(line: str) -> list[str]:
    """Split a reply on whitespace, keeping double-quoted strings whole (without the quotes)."""
    return [m.group(1) if m.group(1) is not None else m.group(2) for m in _TOKEN.finditer(line)]


def to_grams(value: str, unit: str) -> float:
    try:
        factor = _TO_GRAMS[unit]
    except KeyError:
        raise NeedsAttention(
            f"Balance reports weight in unit {unit!r}, which pyreagent does not convert.",
            suggested_action="Ask a human to set the balance display/host unit to grams.",
        ) from None
    return float(value) * factor


class MettlerToledoSICSBackend(BalanceBackend):
    vendor = "Mettler Toledo"
    model = "MT-SICS balance"

    def __init__(
        self,
        port: str | None = None,
        *,
        baudrate: int = 9600,
        host: str | None = None,
        tcp_port: int | None = None,
        transport: Transport | None = None,
        command_timeout_s: float = 5.0,
    ) -> None:
        """
        Args:
            port: Serial port, e.g. ``"COM3"`` or ``"/dev/ttyUSB0"``.
            baudrate: Must match the balance's interface setting.
            host, tcp_port: For balances on Ethernet (or behind a serial server) instead of ``port``.
            transport: Inject a transport directly (tests use ``MockTransport``).
            command_timeout_s: Reply timeout for commands that do not wait for stability.
        """
        self.transport = transport or line_transport(port=port, host=host, tcp_port=tcp_port, baudrate=baudrate)
        self.command_timeout_s = command_timeout_s
        self.serial_number: str | None = None
        self.balance_type: str | None = None

    async def _command(self, command: str, *, timeout_s: float | None = None) -> list[str]:
        raw = await self.transport.query(
            f"{command}\r\n".encode("ascii"), terminator=b"\r\n", timeout_s=timeout_s or self.command_timeout_s
        )
        tokens = tokenize(raw.decode("ascii", errors="replace").strip())
        if not tokens:
            raise CommunicationError(f"Empty reply to MT-SICS command {command!r}.")
        head = tokens[0]
        if head == "ES":
            raise NotSupported(f"Balance does not understand MT-SICS command {command!r} (ES).")
        if head == "ET":
            raise CommunicationError(f"Balance reported a transmission error for {command!r} (ET).")
        if head == "EL":
            raise NotSupported(f"Balance cannot execute {command!r} in its current mode (EL).")
        return tokens

    @staticmethod
    def _range_error(tokens: list[str], action: str) -> None:
        status = tokens[1] if len(tokens) > 1 else ""
        if status == "+":
            raise NeedsAttention(f"Cannot {action}: balance is over range (overload).",
                                 suggested_action="Ask a human to remove load from the pan.")
        if status == "-":
            raise NeedsAttention(f"Cannot {action}: balance is under range.",
                                 suggested_action="Ask a human to check that the weighing pan is fitted and nothing presses on it.")
        if status == "I":
            raise DeviceTimeout(f"Balance could not {action}: busy or not settled.",
                                suggested_action="Check for drafts, vibration or a moving load, then retry.")

    async def connect(self) -> None:
        await self.transport.open()
        tokens = await self._command("@")
        self.serial_number = tokens[2] if len(tokens) > 2 else None
        tokens = await self._command("I2")
        if len(tokens) > 2:
            self._parse_type(tokens[2])

    def _parse_type(self, text: str) -> None:
        parts = text.split()
        if not parts:
            return
        self.balance_type = parts[0]
        for value, unit in zip(parts[1:], parts[2:]):
            try:
                self.capacity_g = to_grams(value, unit.rstrip(","))
                return
            except (ValueError, NeedsAttention):
                continue

    async def disconnect(self) -> None:
        await self.transport.close()

    async def read_weight(self, *, stable: bool, timeout_s: float) -> Weight:
        tokens = await self._command("S" if stable else "SI", timeout_s=timeout_s if stable else None)
        self._range_error(tokens, "weigh")
        if len(tokens) < 4 or tokens[1] not in ("S", "D"):
            raise CommunicationError(f"Unexpected weight reply: {' '.join(tokens)!r}")
        return Weight(grams=to_grams(tokens[2], tokens[3]), stable=tokens[1] == "S")

    async def tare(self, *, timeout_s: float) -> float:
        tokens = await self._command("T", timeout_s=timeout_s)
        self._range_error(tokens, "tare")
        if len(tokens) < 4 or tokens[1] != "S":
            raise CommunicationError(f"Unexpected tare reply: {' '.join(tokens)!r}")
        return to_grams(tokens[2], tokens[3])

    async def zero(self, *, timeout_s: float) -> None:
        tokens = await self._command("Z", timeout_s=timeout_s)
        self._range_error(tokens, "zero")
        if tokens[1:2] != ["A"]:
            raise CommunicationError(f"Unexpected zero reply: {' '.join(tokens)!r}")

    def info(self) -> dict[str, Any]:
        return {
            **super().info(),
            "balance_type": self.balance_type,
            "serial_number": self.serial_number,
            **self.transport.describe(),
        }
