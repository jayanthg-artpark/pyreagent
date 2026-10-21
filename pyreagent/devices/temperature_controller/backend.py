"""Contract every temperature controller driver implements (heat block, chiller, incubator, hot plate)."""

from __future__ import annotations

import abc

from ...core.backend import Backend
from .models import TemperatureReading


class TemperatureControllerBackend(Backend):
    @abc.abstractmethod
    async def set_target(self, target_c: float) -> None:
        """Turn control on and drive towards ``target_c``."""

    @abc.abstractmethod
    async def deactivate(self) -> None:
        """Turn heating/cooling off."""

    @abc.abstractmethod
    async def read(self) -> TemperatureReading: ...
