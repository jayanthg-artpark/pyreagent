from __future__ import annotations

import asyncio
import random

from ....core.errors import NeedsAttention
from ..backend import BalanceBackend
from ..models import Weight


class SimulatedBalanceBackend(BalanceBackend):
    """In-memory balance. Tests and dry runs move mass on and off the pan with ``place``/``remove``."""

    vendor = "pyreagent"
    model = "Simulated balance"
    simulated = True

    def __init__(
        self,
        *,
        capacity_g: float = 220.0,
        readability_g: float = 0.0001,
        noise_g: float = 0.0,
        settle_s: float = 0.05,
        load_g: float = 0.0,
        seed: int | None = None,
    ) -> None:
        self.capacity_g = capacity_g
        self.readability_g = readability_g
        self.noise_g = noise_g
        self.settle_s = settle_s
        self.load_g = load_g
        self._zero_g = 0.0
        self._tare_g = 0.0
        self._rng = random.Random(seed)
        self.connected = False

    # simulation hooks --------------------------------------------------------

    def place(self, grams: float) -> None:
        self.load_g += grams

    def remove(self, grams: float) -> None:
        self.load_g -= grams

    # BalanceBackend ------------------------------------------------------------

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    def _check_range(self) -> None:
        if self.load_g > self.capacity_g:
            raise NeedsAttention(
                f"Balance overloaded ({self.load_g:g} g on a {self.capacity_g:g} g balance).",
                suggested_action="Ask a human to remove the excess load from the pan.",
            )

    def _round(self, grams: float) -> float:
        steps = round(grams / self.readability_g)
        return round(steps * self.readability_g, 10)

    async def read_weight(self, *, stable: bool, timeout_s: float) -> Weight:
        self._check_range()
        if stable:
            await asyncio.sleep(self.settle_s)
        noise = self._rng.gauss(0, self.noise_g) if self.noise_g and not stable else 0.0
        return Weight(grams=self._round(self.load_g - self._zero_g - self._tare_g + noise), stable=stable)

    async def tare(self, *, timeout_s: float) -> float:
        self._check_range()
        await asyncio.sleep(self.settle_s)
        self._tare_g = self.load_g - self._zero_g
        return self._round(self._tare_g)

    async def zero(self, *, timeout_s: float) -> None:
        self._check_range()
        await asyncio.sleep(self.settle_s)
        self._zero_g = self.load_g
        self._tare_g = 0.0
