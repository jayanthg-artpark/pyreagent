"""Contract every shaker driver implements (orbital shakers, plate shakers, vortexers, heater-shakers).

Heating and a labware latch are optional: a driver lists them in ``features``
(``"heating"``, ``"latch"``) and implements the matching methods. The frontend only
calls those when the feature is listed, after checking ranges.
"""

from __future__ import annotations

import abc
from typing import Any

from ...core.backend import Backend
from ...core.errors import NotSupported
from .models import ShakerStatus


class ShakerBackend(Backend):
    features: frozenset[str] = frozenset()
    rpm_range: tuple[float, float] | None = None
    """Speeds the hardware supports; used when the config gives no min_rpm/max_rpm."""
    temperature_range_c: tuple[float, float] | None = None
    """Heater set points (``heating`` feature)."""

    @abc.abstractmethod
    async def start(self, rpm: float) -> None:
        """Start shaking; return once the shaker is running (at speed, if it can tell)."""

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def status(self) -> ShakerStatus: ...

    # -- optional: heating ------------------------------------------------------------------------

    async def set_temperature(self, celsius: float) -> None:
        raise NotSupported(f"{self.vendor} {self.model} cannot heat.")

    async def deactivate_heater(self) -> None:
        raise NotSupported(f"{self.vendor} {self.model} cannot heat.")

    # -- optional: labware latch -------------------------------------------------------------------

    async def open_latch(self) -> None:
        raise NotSupported(f"{self.vendor} {self.model} has no labware latch.")

    async def close_latch(self) -> None:
        raise NotSupported(f"{self.vendor} {self.model} has no labware latch.")

    def info(self) -> dict[str, Any]:
        return {**super().info(), "features": sorted(self.features)}
