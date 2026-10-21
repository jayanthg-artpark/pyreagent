"""Opentrons Temperature Module (GEN1 / GEN2) driven directly over USB serial, without a robot.

Implemented from the Opentrons firmware driver (``api/src/opentrons/drivers/temp_deck``):
115200 baud 8N1, commands end with `` \\r\\n\\r\\n``, replies end with ``ok\\r\\nok\\r\\n``
and contain "error" on failure.

    M104 S<c>   set target (whole degrees)
    M105        temperatures: T:<target|none> C:<current>
    M18         deactivate
    M115        device info: serial:<s> model:<m> version:<v>  (model "temp_deck_v20"+ is GEN2)
"""

from __future__ import annotations

import re
from typing import Any

from ....core.errors import HardwareError
from ....io import SerialTransport, Transport
from ..backend import TemperatureControllerBackend
from ..models import TemperatureReading

_PAIR = re.compile(r"(\S+?):(\S+)")
_MODEL_VERSION = re.compile(r"v(\d+)")


class OpentronsTemperatureModuleUSBBackend(TemperatureControllerBackend):
    vendor = "Opentrons"
    model = "Temperature Module (USB)"

    def __init__(self, port: str | None = None, *, timeout_s: float = 2.0, transport: Transport | None = None) -> None:
        """
        Args:
            port: Serial port of the module, e.g. ``"COM6"`` or ``"/dev/ttyACM1"``.
        """
        if port is None and transport is None:
            raise ValueError("port is required")
        self.transport = transport or SerialTransport(port or "", baudrate=115200)
        self.timeout_s = timeout_s
        self.device_info: dict[str, str] = {}

    @property
    def hardware_generation(self) -> int | None:
        match = _MODEL_VERSION.search(self.device_info.get("model", ""))
        return None if match is None else (2 if int(match.group(1)) >= 20 else 1)

    async def _command(self, gcode: str) -> dict[str, str | None]:
        raw = await self.transport.query(f"{gcode} \r\n\r\n".encode("ascii"), terminator=b"ok\r\nok\r\n", timeout_s=self.timeout_s)
        text = raw.decode("ascii", "replace")
        if "error" in text.lower():
            raise HardwareError(f"Temperature module error for {gcode!r}: {text.strip()}")
        return {k: (None if v.lower() == "none" else v) for k, v in _PAIR.findall(text)}

    async def connect(self) -> None:
        await self.transport.open()
        self.device_info = {k: v for k, v in (await self._command("M115")).items() if v is not None}

    async def disconnect(self) -> None:
        await self.transport.close()

    async def set_target(self, target_c: float) -> None:
        await self._command(f"M104 S{round(target_c)}")

    async def deactivate(self) -> None:
        await self._command("M18")

    async def read(self) -> TemperatureReading:
        reply = await self._command("M105")
        target = None if reply.get("T") is None else float(reply["T"])  # type: ignore[arg-type]
        return TemperatureReading(current_c=float(reply.get("C") or 0.0), target_c=target, active=target is not None)

    def info(self) -> dict[str, Any]:
        return {**super().info(), "hardware_generation": self.hardware_generation, **self.device_info, **self.transport.describe()}
