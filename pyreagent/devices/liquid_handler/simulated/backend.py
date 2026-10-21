from __future__ import annotations

import asyncio
from typing import Any, Iterable

from ....labware import Labware
from ..backend import FEATURES, LiquidHandlerBackend
from ..models import NozzleLayout, PipetteInfo

DEFAULT_PIPETTES = [
    PipetteInfo(name="left", channels=1, min_volume_ul=20, max_volume_ul=300),
    PipetteInfo(name="right", channels=8, min_volume_ul=1, max_volume_ul=20),
]


class SimulatedLiquidHandlerBackend(LiquidHandlerBackend):
    """Executes nothing, records every motion in ``operations`` (the dry-run trace of an agent's plan)."""

    vendor = "pyreagent"
    model = "Simulated liquid handler"
    simulated = True
    features = FEATURES

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
        self.lights = False
        self.liquid_heights_mm: dict[tuple[str, str], float | None] = {}
        """Set from a test: what probe_liquid_heights reports for (slot, well); default 5.0 mm."""

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

    async def blow_out(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, flow_rate_ul_s: float | None) -> None:
        await self._step(op="blow_out", pipette=pipette.name, slot=slot, well=well)

    async def touch_tip(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str) -> None:
        await self._step(op="touch_tip", pipette=pipette.name, slot=slot, well=well)

    async def return_tips(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str) -> None:
        await self._step(op="return_tips", pipette=pipette.name, slot=slot, well=well)

    async def move_to(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, height_mm: float) -> None:
        await self._step(op="move_to", pipette=pipette.name, slot=slot, well=well, height_mm=height_mm)

    async def move_labware(self, *, labware: Labware, from_slot: str, to_slot: str, use_gripper: bool) -> None:
        await self._step(op="move_labware", labware=labware.id, from_slot=from_slot, to_slot=to_slot, use_gripper=use_gripper)

    async def probe_liquid_heights(self, *, pipette: PipetteInfo, slot: str, labware: Labware, wells: list[str]) -> dict[str, float | None]:
        await self._step(op="probe", pipette=pipette.name, slot=slot, wells=list(wells))
        return {w: self.liquid_heights_mm.get((slot, w), 5.0) for w in wells}

    async def configure_nozzles(self, *, pipette: PipetteInfo, layout: NozzleLayout) -> None:
        await self._step(op="configure_nozzles", pipette=pipette.name, layout=layout.model_dump(exclude_none=True))

    async def set_lights(self, on: bool) -> None:
        self.lights = on
