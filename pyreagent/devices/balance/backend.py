"""Contract every balance driver implements."""

from __future__ import annotations

import abc
from typing import Any

from ...core.backend import Backend
from .models import Weight


class BalanceBackend(Backend):
    capacity_g: float | None = None
    """Maximum load, if the driver knows it (often read from the balance on connect)."""

    @abc.abstractmethod
    async def read_weight(self, *, stable: bool, timeout_s: float) -> Weight:
        """Return the net weight. With ``stable`` wait for the balance to settle."""

    @abc.abstractmethod
    async def tare(self, *, timeout_s: float) -> float:
        """Store the current load as tare (after it settles). Return the tare in grams."""

    @abc.abstractmethod
    async def zero(self, *, timeout_s: float) -> None:
        """Set a new zero point (after it settles). Clears tare."""

    def info(self) -> dict[str, Any]:
        return {**super().info(), "capacity_g": self.capacity_g}
