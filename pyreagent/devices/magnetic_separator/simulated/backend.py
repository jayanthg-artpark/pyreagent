from __future__ import annotations

from ..backend import MagneticSeparatorBackend
from ..models import MagnetStatus


class SimulatedMagneticSeparatorBackend(MagneticSeparatorBackend):
    vendor = "pyreagent"
    model = "Simulated magnetic separator"
    simulated = True

    def __init__(self, *, height_range_mm: tuple[float, float] = (-2.5, 22.5)) -> None:
        self.height_range_mm = tuple(height_range_mm)  # type: ignore[assignment]
        self._height: float | None = None

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        self._height = None

    async def engage(self, height_mm: float) -> None:
        self._height = height_mm

    async def disengage(self) -> None:
        self._height = None

    async def status(self) -> MagnetStatus:
        return MagnetStatus(engaged=self._height is not None, height_mm=self._height)
