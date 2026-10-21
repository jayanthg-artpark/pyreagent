"""Contract every plate reader driver implements."""

from __future__ import annotations

import abc
from typing import Any, Callable

from ...core.backend import Backend
from ...labware import Labware


class PlateReaderBackend(Backend):
    absorbance_range_nm: tuple[int, int] = (230, 1000)
    """Wavelengths the optics support; override per instrument."""

    @abc.abstractmethod
    async def open_tray(self) -> None: ...

    @abc.abstractmethod
    async def close_tray(self) -> None: ...

    @abc.abstractmethod
    async def tray_is_open(self) -> bool: ...

    @abc.abstractmethod
    async def read_absorbance(
        self, *, plate: Labware, wavelength_nm: int, wells: list[str], progress: Callable[[float], None]
    ) -> dict[str, float]:
        """Read ``wells`` and return OD per well. Call ``progress`` with 0..1 as the read advances."""

    def info(self) -> dict[str, Any]:
        return {**super().info(), "absorbance_range_nm": list(self.absorbance_range_nm)}
