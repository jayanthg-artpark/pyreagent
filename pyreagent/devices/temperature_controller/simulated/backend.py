from __future__ import annotations

import math
import time

from ..backend import TemperatureControllerBackend
from ..models import TemperatureReading


class SimulatedTemperatureControllerBackend(TemperatureControllerBackend):
    """First-order thermal model: approaches the setpoint (or ambient when off) with time constant ``tau_s``."""

    vendor = "pyreagent"
    model = "Simulated temperature controller"
    simulated = True

    def __init__(self, *, ambient_c: float = 22.0, tau_s: float = 5.0) -> None:
        self.ambient_c = ambient_c
        self.tau_s = tau_s
        self._temp = ambient_c
        self._target: float | None = None
        self._t = time.monotonic()

    def _advance(self) -> None:
        now = time.monotonic()
        goal = self._target if self._target is not None else self.ambient_c
        self._temp = goal + (self._temp - goal) * math.exp(-(now - self._t) / self.tau_s)
        self._t = now

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        await self.deactivate()

    async def set_target(self, target_c: float) -> None:
        self._advance()
        self._target = target_c

    async def deactivate(self) -> None:
        self._advance()
        self._target = None

    async def read(self) -> TemperatureReading:
        self._advance()
        return TemperatureReading(current_c=round(self._temp, 3), target_c=self._target, active=self._target is not None)
