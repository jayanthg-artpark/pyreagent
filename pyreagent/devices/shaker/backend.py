"""Contract every shaker driver implements (orbital shakers, plate shakers, vortexers)."""

from __future__ import annotations

import abc

from ...core.backend import Backend
from .models import ShakerStatus


class ShakerBackend(Backend):
    @abc.abstractmethod
    async def start(self, rpm: float) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def status(self) -> ShakerStatus: ...
