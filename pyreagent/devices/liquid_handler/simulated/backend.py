from __future__ import annotations

import asyncio
from typing import Any, Iterable

from ....labware import Labware
from ..backend import LiquidHandlerBackend
from ..models import PipetteInfo

DEFAULT_PIPETTES = [
    PipetteInfo(name="left", channels=1, min_volume_ul=20, max_volume_ul=300),
    PipetteInfo(name="right", channels=8, min_volume_ul=1, max_volume_ul=20),
]


class SimulatedLiquidHandlerBackend(LiquidHandlerBackend):
    """Executes nothing, records every motion in ``operations`` (the dry-run trace of an agent's plan)."""

    vendor = "pyreagent"
    model = "Simulated liquid handler"
    simulated = True

    def __init__(
        self,
        *,
        slots: list[str] | None = None,
        pipettes: Iterable[PipetteInfo | dict[str, Any]] | None = None,
        seconds_per_operation: float = 0.0,
    ) -> None:
        self.slots = list(slots or [str(i) for i in range(1, 12)])
        infos = [PipetteInfo.model_validate(p) for p in (pipettes if pipettes is not None else DEFAULT_PIPETTES)]
        self._pipettes = {p.name: p for p in infos}
        if len(self._pipettes) != len(infos):
            raise ValueError("pipette names must be unique")
        self.seconds_per_operation = seconds_per_operation
        self.operations: list[dict[str, Any]] = []
        self.homed = False

    @property
    def pipettes(self) -> dict[str, PipetteInfo]:
        return self._pipettes

    async def _step(self, **record: Any) -> None:
        if self.seconds_per_operation:
            await asyncio.sleep(self.seconds_per_operation)
        self.operations.append(record)

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def home(self) -> None:
        await self._step(op="home")
        self.homed = True

    async def pick_up_tips(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str) -> None:
        await self._step(op="pick_up", pipette=pipette.name, slot=slot, well=well)

    async def drop_tips(self, *, pipette: PipetteInfo) -> None:
        await self._step(op="drop", pipette=pipette.name)

    async def aspirate(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, volume_ul: float,
                       flow_rate_ul_s: float | None) -> None:
        await self._step(op="aspirate", pipette=pipette.name, slot=slot, well=well, volume_ul=round(volume_ul, 4))

    async def dispense(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, volume_ul: float,
                       flow_rate_ul_s: float | None) -> None:
        await self._step(op="dispense", pipette=pipette.name, slot=slot, well=well, volume_ul=round(volume_ul, 4))

    async def stop(self) -> None:
        self.operations.append({"op": "stop"})
