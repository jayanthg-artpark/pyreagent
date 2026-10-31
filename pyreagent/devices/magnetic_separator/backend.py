"""Contract every magnetic separator driver implements (raise and lower magnets under a plate)."""

from __future__ import annotations

import abc
from typing import Any

from ...core.backend import Backend
from .models import MagnetStatus


class MagneticSeparatorBackend(Backend):
    height_range_mm: tuple[float, float] = (0.0, 20.0)
    """Engage heights the hardware reaches, mm above the surface the labware rests on."""

    @abc.abstractmethod
    async def engage(self, height_mm: float) -> None: ...

    @abc.abstractmethod
    async def disengage(self) -> None: ...

    @abc.abstractmethod
    async def status(self) -> MagnetStatus: ...

    def info(self) -> dict[str, Any]:
        return {**super().info(), "height_range_mm": list(self.height_range_mm)}
