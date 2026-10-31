"""Opentrons Heater-Shaker Module, through the robot it is plugged into (OT-2 or Flex).

Shares the robot's run with the pipettes, so labware on the module can be used by the
Opentrons liquid handler device. Commands are ``heaterShaker/*``; speed, temperature and
latch come from ``GET /modules``. Speeds 200-3000 rpm; the heater only heats, 37-95 °C.

The robot only shakes once the run knows the labware latch is closed. A new run doesn't
know, so if the hardware reports the latch closed the driver tells the run so (a close
command on a closed latch does not move) before it starts shaking.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping

import httpx

from ....vendors.opentrons.client import DEFAULT_PORT
from ....vendors.opentrons.modules import ModuleLink
from ..backend import ShakerBackend
from ..models import ShakerStatus


def latch_state(value: str | None) -> Literal["open", "closed", "unknown"]:
    """``GET /modules`` latch status; ``opening``, ``closing`` and ``idle_unknown`` count as unknown."""
    return {"idle_closed": "closed", "idle_open": "open"}.get(value or "", "unknown")  # type: ignore[return-value]


class OpentronsHeaterShakerBackend(ShakerBackend):
    vendor = "Opentrons"
    model = "Heater-Shaker Module (via robot)"
    features = frozenset({"heating", "latch"})
    rpm_range = (200.0, 3000.0)
    temperature_range_c = (37.0, 95.0)

    def __init__(
        self,
        host: str | None = None,
        *,
        robot: str,
        slot: str,
        port: int = DEFAULT_PORT,
        serial: str | None = None,
        headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Args:
            host, robot: The robot the module is plugged into (``"ot2"`` or ``"flex"``).
            slot: Deck slot the module sits in (Flex: columns 1 and 3, in its caddy).
            serial: Module serial number, needed only if several heater-shakers are attached.
        """
        self.module = ModuleLink("heater_shaker", host=host, robot=robot, slot=slot, port=port, serial=serial,
                                 headers=headers, transport=transport)
        self._latched_in_run = -1  # run generation in which the latch was last commanded closed

    async def connect(self) -> None:
        await self.module.connect()

    async def disconnect(self) -> None:
        await self.module.disconnect()

    async def start(self, rpm: float) -> None:
        if self._latched_in_run != self.module.generation and latch_state((await self.module.data()).get("labwareLatchStatus")) == "closed":
            await self.close_latch()
        await self.module.command("heaterShaker/setAndWaitForShakeSpeed", rpm=round(rpm))

    async def stop(self) -> None:
        await self.module.command("heaterShaker/deactivateShaker")

    async def set_temperature(self, celsius: float) -> None:
        await self.module.command("heaterShaker/setTargetTemperature", celsius=celsius)

    async def deactivate_heater(self) -> None:
        await self.module.command("heaterShaker/deactivateHeater")

    async def open_latch(self) -> None:
        await self.module.command("heaterShaker/openLabwareLatch")
        self._latched_in_run = -1

    async def close_latch(self) -> None:
        await self.module.command("heaterShaker/closeLabwareLatch")
        self._latched_in_run = self.module.generation

    async def status(self) -> ShakerStatus:
        data = await self.module.data()
        speed = float(data.get("currentSpeed") or 0)
        return ShakerStatus(
            shaking=speed > 0 or data.get("targetSpeed") is not None, rpm=speed,
            temperature_c=data.get("currentTemperature"), target_temperature_c=data.get("targetTemperature"),
            latch=latch_state(data.get("labwareLatchStatus")),
        )

    def info(self) -> dict[str, Any]:
        return {**super().info(), **self.module.info()}
