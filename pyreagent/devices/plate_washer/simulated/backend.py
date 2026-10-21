from __future__ import annotations

import asyncio

from ....labware import Labware
from ..backend import PlateWasherBackend, Progress
from ..models import PlateWasherStatus


class SimulatedPlateWasherBackend(PlateWasherBackend):
    """Tracks buffer use and waste so supply checks can be exercised."""

    vendor = "pyreagent"
    model = "Simulated plate washer"
    simulated = True

    def __init__(
        self,
        *,
        buffers: dict[str, float] | None = None,
        waste_capacity_ml: float = 4000.0,
        max_dispense_ul: float = 400.0,
        plate_formats: tuple[int, ...] = (96, 384),
        seconds_per_column: float = 0.01,
    ) -> None:
        """``buffers`` maps bottle name to its starting volume in mL."""
        self.levels = dict(buffers or {"A": 2000.0, "B": 2000.0})
        self.buffers = list(self.levels)
        self.waste_ml = 0.0
        self.waste_capacity_ml = waste_capacity_ml
        self.max_dispense_ul = max_dispense_ul
        self.plate_formats = tuple(plate_formats)
        self.seconds_per_column = seconds_per_column

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def _tick(self) -> None:
        await asyncio.sleep(self.seconds_per_column)

    async def prime(self, *, buffer: str, volume_ml: float) -> None:
        await self._tick()
        self.levels[buffer] -= volume_ml
        self.waste_ml += volume_ml

    async def wash(self, *, plate: Labware, columns: list[int], buffer: str, cycles: int, dispense_volume_ul: float,
                   soak_s: float, final_aspirate: bool, progress: Progress) -> None:
        per_column_ml = plate.rows * dispense_volume_ul / 1000
        for cycle in range(1, cycles + 1):
            for index, _column in enumerate(columns, start=1):
                await self._tick()
                self.levels[buffer] -= per_column_ml
                if cycle > 1:
                    self.waste_ml += per_column_ml  # aspirated the previous cycle's buffer
                progress(((cycle - 1) * len(columns) + index) / (cycles * len(columns)), f"cycle {cycle}/{cycles}")
            if soak_s:
                await asyncio.sleep(min(soak_s, 0.01))
        if final_aspirate:
            self.waste_ml += per_column_ml * len(columns)

    async def dispense(self, *, plate: Labware, columns: list[int], buffer: str, volume_ul: float, progress: Progress) -> None:
        for index, _column in enumerate(columns, start=1):
            await self._tick()
            self.levels[buffer] -= plate.rows * volume_ul / 1000
            progress(index / len(columns), f"column {index}/{len(columns)}")

    async def stop(self) -> None:
        pass

    async def status(self) -> PlateWasherStatus:
        return PlateWasherStatus(
            buffers_ml={k: round(v, 3) for k, v in self.levels.items()},
            waste_ml=round(self.waste_ml, 3),
            waste_capacity_ml=self.waste_capacity_ml,
            waste_full=self.waste_ml >= self.waste_capacity_ml,
        )
