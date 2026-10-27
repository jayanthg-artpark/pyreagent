from __future__ import annotations

import asyncio
from typing import Literal

from ..backend import ShakerBackend
from ..models import ShakerStatus


class SimulatedShakerBackend(ShakerBackend):
    """In-memory shaker. With ``heating`` and ``latch`` it behaves like a heater-shaker (e.g. the Opentrons one)."""

    vendor = "pyreagent"
    model = "Simulated shaker"
    simulated = True

    def __init__(self, *, heating: bool = False, latch: bool = False, temperature_range_c: tuple[float, float] = (37.0, 95.0),
                 heat_c_per_s: float = 0.5) -> None:
        self.features = frozenset(f for f, on in (("heating", heating), ("latch", latch)) if on)
        self.temperature_range_c = tuple(temperature_range_c) if heating else None  # type: ignore[assignment]
        self.heat_c_per_s = heat_c_per_s
        self._rpm = 0.0
        self._latch: Literal["open", "closed", "unknown"] = "unknown"
        self._target: float | None = None
        self._temperature = 23.0
        self._since = 0.0

    async def connect(self) -> None:
        self._since = asyncio.get_running_loop().time()

    async def disconnect(self) -> None:
        await self.stop()
        self._target = None

    async def start(self, rpm: float) -> None:
        self._rpm = rpm

    async def stop(self) -> None:
        self._rpm = 0.0

    def _settle(self) -> None:
        now = asyncio.get_running_loop().time()
        goal = self._target if self._target is not None else 23.0
        step = self.heat_c_per_s * (now - self._since)
        self._temperature = goal if abs(goal - self._temperature) <= step else self._temperature + step * (1 if goal > self._temperature else -1)
        self._since = now

    async def set_temperature(self, celsius: float) -> None:
        self._settle()
        self._target = celsius

    async def deactivate_heater(self) -> None:
        self._settle()
        self._target = None

    async def open_latch(self) -> None:
        self._latch = "open"

    async def close_latch(self) -> None:
        self._latch = "closed"

    async def status(self) -> ShakerStatus:
        heating = "heating" in self.features
        if heating:
            self._settle()
        return ShakerStatus(shaking=self._rpm > 0, rpm=self._rpm,
                            temperature_c=round(self._temperature, 2) if heating else None,
                            target_temperature_c=self._target if heating else None,
                            latch=self._latch if "latch" in self.features else None)
