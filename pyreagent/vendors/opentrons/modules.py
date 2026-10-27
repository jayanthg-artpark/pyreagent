"""What every Opentrons module driver does with the robot's shared run: join it, load the module, send commands."""

from __future__ import annotations

from typing import Any, Mapping

import httpx

from .client import DEFAULT_PORT
from .session import OpentronsSession


class ModuleLink:
    """One module, in one deck slot, of the robot whose run it shares with the pipettes and other modules."""

    def __init__(
        self,
        kind: str,
        *,
        host: str | None,
        robot: str,
        slot: str,
        port: int = DEFAULT_PORT,
        serial: str | None = None,
        headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.session = OpentronsSession.get(host=host, robot=robot, port=port, headers=headers, transport=transport)
        self.slot = slot
        self.session.register_module(kind, slot, serial=serial)
        self._user = f"{kind}@{slot}"

    async def connect(self) -> None:
        await self.session.acquire(self._user)
        try:
            await self.session.module_id(self.slot)
        except BaseException:
            await self.session.release(self._user)
            raise

    async def disconnect(self) -> None:
        await self.session.release(self._user)

    @property
    def generation(self) -> int:
        """Changes when the robot's run is replaced (after a stop): module state set in the old run is gone."""
        return self.session.generation

    async def command(self, command_type: str, **params: Any) -> dict[str, Any]:
        module_id = await self.session.module_id(self.slot)
        return await self.session.command(command_type, {"moduleId": module_id, **params})

    async def data(self) -> dict[str, Any]:
        """Live module data from ``GET /modules``."""
        return await self.session.module_status(self.slot)

    def info(self) -> dict[str, Any]:
        registration = self.session.modules[self.slot]
        return {"robot": self.session.robot, "url": self.session.client.base_url, "slot": self.slot,
                "module_model": registration.model, "serial": registration.serial}
