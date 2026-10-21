"""Contract every plate washer driver implements."""

from __future__ import annotations

import abc
from typing import Any, Callable

from ...core.backend import Backend
from ...labware import Labware
from .models import PlateWasherStatus

Progress = Callable[[float, str], None]


class PlateWasherBackend(Backend):
    buffers: list[str] = []
    """Buffer bottle / inlet valve names, e.g. ["A", "B"]."""
    max_dispense_ul: float = 400.0
    plate_formats: tuple[int, ...] = (96,)
    """Well counts the manifold fits."""

    @abc.abstractmethod
    async def prime(self, *, buffer: str, volume_ml: float) -> None:
        """Fill the lines with ``buffer`` into the priming trough."""

    @abc.abstractmethod
    async def wash(
        self, *, plate: Labware, columns: list[int], buffer: str, cycles: int, dispense_volume_ul: float,
        soak_s: float, final_aspirate: bool, progress: Progress,
    ) -> None:
        """Each cycle: aspirate, dispense, optional soak. Then a final aspirate if asked."""

    @abc.abstractmethod
    async def dispense(self, *, plate: Labware, columns: list[int], buffer: str, volume_ul: float, progress: Progress) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def status(self) -> PlateWasherStatus: ...

    def info(self) -> dict[str, Any]:
        return {**super().info(), "buffers": list(self.buffers), "max_dispense_ul": self.max_dispense_ul,
                "plate_formats": list(self.plate_formats)}
