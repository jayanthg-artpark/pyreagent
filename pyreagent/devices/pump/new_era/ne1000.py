"""New Era Pump Systems syringe pumps (NE-1000 family) over RS-232, basic mode.

Implemented from the NE-1000 user manual's RS-232 protocol. Commands are
ASCII, optionally prefixed by the pump's network address, terminated by CR.
Replies are framed ``STX <2-digit address> <status> [data] ETX`` where status is

    I infusing   W withdrawing   S stopped   P paused   T timed pause
    U waiting for trigger         X purging   A alarm (data = ?<code>)

and data starting with ``?`` is an error: ``?`` unknown command, ``?NA`` not
applicable now, ``?OOR`` out of range, ``?COM`` bad packet, ``?IGN`` ignored.

Commands used: ``VER``, ``DIA <mm>``, ``VOL ML``, ``DIR INF|WDR``,
``RAT <x> MM|UM``, ``VOL <x>``, ``CLD INF|WDR``, ``RUN``, ``STP``, ``DIS``
(volume dispensed: ``I<infused>W<withdrawn><UL|ML>``).

Numbers are limited to 4 digits plus a decimal point, so small rates are sent
in µL/min to keep resolution. Valid ranges depend on the syringe diameter; the
pump answers ``?OOR`` outside them, which surfaces as ``invalid_argument``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ....core.errors import CommunicationError, InvalidArgument, NotSupported
from ....io import Transport, line_transport
from ..backend import PumpBackend
from ..models import PumpDirection, PumpStatus

STX, ETX = b"\x02", b"\x03"

ALARMS = {
    "R": "pump was reset (power interrupted)",
    "S": "motor stalled",
    "T": "safe-mode communication timeout",
    "E": "pumping program error",
    "O": "pumping program phase out of range",
}

_DISPENSED = re.compile(r"I([\d.]+)W([\d.]+)(UL|ML)")


@dataclass
class Reply:
    address: int
    status: str
    data: str

    @property
    def alarm(self) -> str | None:
        if self.status != "A":
            return None
        code = self.data.lstrip("?")[:1]
        return ALARMS.get(code, f"alarm {self.data!r}")


def parse_reply(raw: bytes) -> Reply:
    text = raw.decode("ascii", errors="replace")
    start = text.rfind("\x02")
    body = text[start + 1 :].rstrip("\x03\r\n") if start >= 0 else text.strip("\x03\r\n")
    if len(body) < 3 or not body[:2].isdigit():
        raise CommunicationError(f"Malformed pump reply {raw!r}.")
    return Reply(address=int(body[:2]), status=body[2], data=body[3:])


def format_number(value: float) -> str:
    """Render ``value`` in at most 4 digits with up to 3 decimals, as the pump requires."""
    if not 0 < value < 9999.5:
        raise InvalidArgument(f"{value} cannot be sent to the pump (must be > 0 and < 10000 in the chosen unit).")
    for decimals in (3, 2, 1, 0):
        text = f"{value:.{decimals}f}"
        if sum(ch.isdigit() for ch in text) <= 4:
            if float(text) == 0:
                break
            return text
    raise InvalidArgument(f"{value} is below the pump's resolution in the chosen unit.")


class NewEraSyringePumpBackend(PumpBackend):
    vendor = "New Era Pump Systems"
    model = "NE-1000 series syringe pump"

    def __init__(
        self,
        port: str | None = None,
        *,
        syringe_diameter_mm: float,
        address: int = 0,
        baudrate: int = 19200,
        host: str | None = None,
        tcp_port: int | None = None,
        transport: Transport | None = None,
        timeout_s: float = 2.0,
    ) -> None:
        """
        Args:
            port: Serial port of the pump (or the first pump in a daisy chain).
            syringe_diameter_mm: Inside diameter of the fitted syringe, from the syringe maker's table.
            address: Pump network address (0-99), as set on the pump.
            baudrate: Must match the pump setting (factory default 19200).
        """
        if not 0 <= address <= 99:
            raise ValueError("address must be 0..99")
        self.transport = transport or line_transport(port=port, host=host, tcp_port=tcp_port, baudrate=baudrate)
        self.syringe_diameter_mm = syringe_diameter_mm
        self.address = address
        self.timeout_s = timeout_s
        self.firmware: str | None = None
        self._direction: PumpDirection | None = None

    async def _send(self, command: str) -> Reply:
        raw = await self.transport.query(
            f"{self.address:02d}{command}\r".encode("ascii"), terminator=ETX, timeout_s=self.timeout_s
        )
        reply = parse_reply(raw)
        if reply.status != "A" and reply.data.startswith("?"):
            code = reply.data[1:]
            if code == "OOR":
                raise InvalidArgument(f"Pump rejected {command!r}: out of range for the fitted syringe.")
            if code == "NA":
                raise InvalidArgument(f"Pump rejected {command!r}: not applicable in its current state.")
            if code in ("COM", "IGN"):
                raise CommunicationError(f"Pump did not accept {command!r} ({code}).")
            raise NotSupported(f"Pump does not recognise {command!r}.")
        return reply

    async def connect(self) -> None:
        await self.transport.open()
        reply = await self._send("VER")
        if reply.status == "A":
            # The first command after power-up reports the reset alarm; ask again.
            reply = await self._send("VER")
        self.firmware = reply.data or None
        await self._send(f"DIA {format_number(self.syringe_diameter_mm)}")
        await self._send("VOL ML")

    async def disconnect(self) -> None:
        try:
            await self.stop()
        finally:
            await self.transport.close()

    async def start(self, *, direction: PumpDirection, volume_ml: float, rate_ml_min: float) -> None:
        await self._send("DIR INF" if direction is PumpDirection.INFUSE else "DIR WDR")
        if rate_ml_min < 1:
            await self._send(f"RAT {format_number(rate_ml_min * 1000)} UM")
        else:
            await self._send(f"RAT {format_number(rate_ml_min)} MM")
        await self._send(f"VOL {format_number(volume_ml)}")
        await self._send("CLD INF")
        await self._send("CLD WDR")
        self._direction = direction
        await self._send("RUN")

    async def stop(self) -> None:
        await self._send("STP")

    async def status(self) -> PumpStatus:
        reply = await self._send("DIS")
        match = _DISPENSED.search(reply.data)
        if match is None and reply.alarm is None:
            raise CommunicationError(f"Unexpected DIS reply {reply.data!r}.")
        infused = withdrawn = 0.0
        if match:
            scale = 1e-3 if match.group(3) == "UL" else 1.0
            infused, withdrawn = float(match.group(1)) * scale, float(match.group(2)) * scale
        direction = {"I": PumpDirection.INFUSE, "W": PumpDirection.WITHDRAW}.get(reply.status, self._direction)
        return PumpStatus(
            running=reply.status in ("I", "W", "X"),
            direction=direction,
            infused_ml=infused,
            withdrawn_ml=withdrawn,
            alarm=reply.alarm,
        )

    def info(self) -> dict[str, Any]:
        return {
            **super().info(),
            "firmware": self.firmware,
            "address": self.address,
            "syringe_diameter_mm": self.syringe_diameter_mm,
            **self.transport.describe(),
        }
