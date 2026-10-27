"""Opentrons Magnetic Module (GEN1 / GEN2), through the OT-2 it is plugged into.

Shares the robot's run with the pipettes, so the plate on the module is labware the Opentrons
liquid handler device can use. Commands are ``magneticModule/*``; state comes from
``GET /modules``. Heights are true millimetres of the magnet tops above the ledge the labware
rests on (negative: below it): -2.5 to 20 mm on GEN1, -2.5 to 22.5 mm on GEN2. The model is
read from the robot on connect.

The Flex uses the passive Magnetic Block instead: declare it on the liquid handler
(``magnetic_blocks``), as it has nothing to drive.
"""

from __future__ import annotations

from typing import Any, Mapping

import httpx

from ....vendors.opentrons.client import DEFAULT_PORT
from ....vendors.opentrons.modules import ModuleLink
from ..backend import MagneticSeparatorBackend
from ..models import MagnetStatus

HEIGHT_RANGES_MM = {"magneticModuleV1": (-2.5, 20.0), "magneticModuleV2": (-2.5, 22.5)}


class OpentronsMagneticModuleBackend(MagneticSeparatorBackend):
    vendor = "Opentrons"
    model = "Magnetic Module (via robot)"
    height_range_mm = HEIGHT_RANGES_MM["magneticModuleV1"]  # the narrower one until the model is known

    def __init__(
        self,
        host: str | None = None,
        *,
        slot: str,
        robot: str = "ot2",
        port: int = DEFAULT_PORT,
        serial: str | None = None,
        headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Args:
            host: The OT-2 the module is plugged into.
            slot: Its deck slot.
            serial: Module serial number, needed only if several magnetic modules are attached.
        """
        self.module = ModuleLink("magnetic", host=host, robot=robot, slot=slot, port=port, serial=serial,
                                 headers=headers, transport=transport)

    async def connect(self) -> None:
        await self.module.connect()
        model = self.module.session.modules[self.module.slot].model or ""
        self.height_range_mm = HEIGHT_RANGES_MM.get(model, self.height_range_mm)

    async def disconnect(self) -> None:
        await self.module.disconnect()

    async def engage(self, height_mm: float) -> None:
        await self.module.command("magneticModule/engage", height=height_mm)

    async def disengage(self) -> None:
        await self.module.command("magneticModule/disengage")

    async def status(self) -> MagnetStatus:
        data = await self.module.data()
        engaged = bool(data.get("engaged"))
        return MagnetStatus(engaged=engaged, height_mm=data.get("height") if engaged else None)

    def info(self) -> dict[str, Any]:
        return {**super().info(), **self.module.info()}
