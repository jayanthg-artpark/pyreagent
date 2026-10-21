"""Opentrons Temperature Module (GEN1 / GEN2), through the robot it is plugged into.

Shares the robot's run with the pipettes, so labware on the module can be
pipetted into by the Opentrons liquid handler device. Commands are
``temperatureModule/*``; temperatures come from ``GET /modules``. The robot
rounds setpoints to whole degrees.
"""

from __future__ import annotations

from typing import Any, Mapping

import httpx

from ....vendors.opentrons.client import DEFAULT_PORT
from ....vendors.opentrons.session import OpentronsSession
from ..backend import TemperatureControllerBackend
from ..models import TemperatureReading


class OpentronsTemperatureModuleBackend(TemperatureControllerBackend):
    vendor = "Opentrons"
    model = "Temperature Module (via robot)"

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
            slot: Deck slot the module sits in (Flex: columns 1 and 3).
            serial: Module serial number, needed only if several temperature modules are attached.
        """
        self.session = OpentronsSession.get(host=host, robot=robot, port=port, headers=headers, transport=transport)
        self.slot = slot
        self.session.register_module("temperature", slot, serial=serial)
        self._user = f"temperature@{slot}"

    async def connect(self) -> None:
        await self.session.acquire(self._user)
        try:
            await self.session.module_id(self.slot)
        except BaseException:
            await self.session.release(self._user)
            raise

    async def disconnect(self) -> None:
        await self.session.release(self._user)

    async def _command(self, command_type: str, **params: Any) -> None:
        module_id = await self.session.module_id(self.slot)
        await self.session.command(command_type, {"moduleId": module_id, **params})

    async def set_target(self, target_c: float) -> None:
        await self._command("temperatureModule/setTargetTemperature", celsius=round(target_c))

    async def deactivate(self) -> None:
        await self._command("temperatureModule/deactivate")

    async def read(self) -> TemperatureReading:
        data = await self.session.module_status(self.slot)
        target = data.get("targetTemperature")
        return TemperatureReading(current_c=data.get("currentTemperature") or 0.0, target_c=target, active=target is not None)

    def info(self) -> dict[str, Any]:
        registration = self.session.modules[self.slot]
        return {**super().info(), "robot": self.session.robot, "url": self.session.client.base_url, "slot": self.slot,
                "module_model": registration.model, "serial": registration.serial}
