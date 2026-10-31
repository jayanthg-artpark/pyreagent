"""Contract every plate reader driver implements.

A driver describes its instrument in ``spec`` (a ``ReaderSpec``) and implements the
methods for what the spec offers. The frontend only calls a method when the spec
allows it, after checking every argument against the spec's limits, so drivers
need not repeat those checks. Methods for things the instrument cannot do keep
their default, which refuses.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel

from ...core.backend import Backend
from ...core.errors import NotSupported
from ...labware import Labware
from .models import Optics, ReaderSpec, ShakeMode

Progress = Callable[[float], None]
"""Call with 0..1 as a read advances."""


class FluorescenceSettings(BaseModel):
    excitation_nm: int
    emission_nm: int
    optics: Optics = "top"
    gain: int | None = None
    """None: the instrument's automatic/default gain."""
    focal_height_mm: float | None = None
    flashes: int | None = None


class PlateReaderBackend(Backend):
    spec: ReaderSpec = ReaderSpec()
    """What this instrument can do. A property in drivers that learn the model on connect."""

    def _unsupported(self, what: str) -> NotSupported:
        return NotSupported(f"{self.vendor} {self.model} cannot {what}.")

    # -- tray ------------------------------------------------------------------------------------

    async def open_tray(self) -> None:
        raise self._unsupported("open a tray")

    async def close_tray(self) -> None:
        raise self._unsupported("close a tray")

    async def tray_is_open(self) -> bool | None:
        """``None`` if the instrument has no tray (or cannot tell)."""
        return None

    # -- measurements ------------------------------------------------------------------------------

    async def read_absorbance(self, *, plate: Labware, wavelength_nm: int, wells: list[str], progress: Progress) -> dict[str, float]:
        """OD per well at one wavelength."""
        raise self._unsupported("read absorbance")

    async def read_absorbance_spectrum(
        self, *, plate: Labware, wavelengths_nm: list[int], wells: list[str], progress: Progress
    ) -> dict[str, list[float]]:
        """OD per well at each wavelength, in order."""
        raise self._unsupported("read an absorbance spectrum")

    async def read_fluorescence(
        self, *, plate: Labware, settings: FluorescenceSettings, wells: list[str], progress: Progress
    ) -> tuple[dict[str, float], int | None]:
        """RFU per well, and the gain actually used (None if the instrument does not report it)."""
        raise self._unsupported("read fluorescence")

    async def read_luminescence(
        self, *, plate: Labware, integration_s: float, focal_height_mm: float | None, wells: list[str], progress: Progress
    ) -> dict[str, float]:
        """RLU per well."""
        raise self._unsupported("read luminescence")

    async def abort_read(self) -> None:
        """Stop a read in progress (called when its job is cancelled). Default: let it finish."""

    # -- incubation and shaking -------------------------------------------------------------------

    async def set_temperature(self, celsius: float) -> None:
        raise self._unsupported("control temperature")

    async def deactivate_temperature(self) -> None:
        raise self._unsupported("control temperature")

    async def get_temperature(self) -> tuple[float | None, float | None]:
        """(current, target) in °C; None where unknown or off."""
        return None, None

    async def shake(self, *, mode: ShakeMode, rpm: int | None, duration_s: float) -> None:
        """Shake for ``duration_s`` and return when done."""
        raise self._unsupported("shake")

    async def stop_shaking(self) -> None:
        """Stop a shake early (called when its job is cancelled)."""

    def info(self) -> dict[str, Any]:
        return {**super().info(), "spec": self.spec.model_dump(exclude_none=True)}
