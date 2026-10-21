from __future__ import annotations

import time

from ..backend import PumpBackend
from ..models import PumpDirection, PumpStatus


class SimulatedPumpBackend(PumpBackend):
    """Pump that moves liquid on the wall clock. ``speedup`` compresses time for tests."""

    vendor = "pyreagent"
    model = "Simulated pump"
    simulated = True

    def __init__(self, *, speedup: float = 1.0) -> None:
        self.speedup = speedup
        self.alarm: str | None = None
        """Set from a test to simulate a stall or other alarm."""
        self._direction: PumpDirection | None = None
        self._volume = 0.0
        self._rate = 0.0
        self._t0: float | None = None
        self._frozen: float | None = None

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        await self.stop()

    def _moved(self) -> float:
        if self._frozen is not None:
            return self._frozen
        if self._t0 is None:
            return 0.0
        elapsed_min = (time.monotonic() - self._t0) * self.speedup / 60
        return min(self._volume, self._rate * elapsed_min)

    async def start(self, *, direction: PumpDirection, volume_ml: float, rate_ml_min: float) -> None:
        self._direction, self._volume, self._rate = direction, volume_ml, rate_ml_min
        self._t0 = time.monotonic()
        self._frozen = None

    async def stop(self) -> None:
        if self._t0 is not None and self._frozen is None:
            self._frozen = self._moved()

    async def status(self) -> PumpStatus:
        moved = self._moved()
        if self.alarm and self._frozen is None:
            self._frozen = moved
        running = self._t0 is not None and self._frozen is None and moved < self._volume
        return PumpStatus(
            running=running,
            direction=self._direction,
            infused_ml=round(moved, 6) if self._direction is PumpDirection.INFUSE else 0.0,
            withdrawn_ml=round(moved, 6) if self._direction is PumpDirection.WITHDRAW else 0.0,
            alarm=self.alarm,
        )
