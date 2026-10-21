"""Contract every pump driver implements (syringe or peristaltic)."""

from __future__ import annotations

import abc

from ...core.backend import Backend
from .models import PumpDirection, PumpStatus


class PumpBackend(Backend):
    @abc.abstractmethod
    async def start(self, *, direction: PumpDirection, volume_ml: float, rate_ml_min: float) -> None:
        """Reset the moved-volume counters and start moving ``volume_ml`` at ``rate_ml_min``.

        Return as soon as the pump is running; the frontend polls ``status`` until it stops.
        """

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop immediately."""

    @abc.abstractmethod
    async def status(self) -> PumpStatus:
        """Running flag and the volumes moved since the last ``start``."""
