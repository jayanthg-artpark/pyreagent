from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Mapping

from ....labware import Labware
from ..backend import FluorescenceSettings, PlateReaderBackend, Progress
from ..models import AbsorbanceSpec, FluorescenceSpec, LuminescenceSpec, ReaderSpec, ShakeMode, ShakeSpec

MULTIMODE = ReaderSpec(
    absorbance=AbsorbanceSpec(min_nm=230, max_nm=1000, spectrum=True, max_spectrum_points=200),
    fluorescence=FluorescenceSpec(min_excitation_nm=250, max_excitation_nm=900, min_emission_nm=280, max_emission_nm=900,
                                  min_stokes_shift_nm=15, min_gain=0, max_gain=255, optics=["top", "bottom"],
                                  focal_height_mm=(1.0, 25.0), max_flashes=100),
    luminescence=LuminescenceSpec(min_integration_s=0.01, max_integration_s=20.0, focal_height_mm=(1.0, 25.0)),
    temperature_c=(20.0, 45.0),
    shaking=ShakeSpec(modes=["orbital", "linear", "double_orbital"], min_rpm=100, max_rpm=1000),
)


def _unit(*key: object) -> float:
    """A repeatable number in 0..1 for the given key."""
    digest = hashlib.sha256("/".join(map(str, key)).encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32


class SimulatedPlateReaderBackend(PlateReaderBackend):
    """Returns repeatable pseudo-random values, so agent plans can be checked end to end.

    By default a multimode reader that can do everything; pass ``spec`` (a ``ReaderSpec``
    as a mapping) to imitate a particular instrument.
    """

    vendor = "pyreagent"
    model = "Simulated plate reader"
    simulated = True

    def __init__(self, *, seconds_per_well: float = 0.01, tray_s: float = 0.05, heat_c_per_s: float = 1.0,
                 spec: Mapping[str, Any] | None = None) -> None:
        self.seconds_per_well = seconds_per_well
        self.tray_s = tray_s
        self.heat_c_per_s = heat_c_per_s
        self.spec = ReaderSpec.model_validate(spec) if spec is not None else MULTIMODE
        self._tray_open = False
        self._target: float | None = None
        self._temperature = 22.0
        self._since = 0.0

    async def connect(self) -> None:
        self._since = asyncio.get_running_loop().time()

    async def disconnect(self) -> None:
        self._target = None

    # -- tray --------------------------------------------------------------------------------------

    async def open_tray(self) -> None:
        await asyncio.sleep(self.tray_s)
        self._tray_open = True

    async def close_tray(self) -> None:
        await asyncio.sleep(self.tray_s)
        self._tray_open = False

    async def tray_is_open(self) -> bool | None:
        return self._tray_open if self.spec.tray else None

    # -- reads -------------------------------------------------------------------------------------

    async def _per_well(self, wells: list[str], progress: Progress, value: Any) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for index, well in enumerate(wells, start=1):
            if self.seconds_per_well:
                await asyncio.sleep(self.seconds_per_well)
            values[well] = value(well)
            progress(index / len(wells))
        return values

    async def read_absorbance(self, *, plate: Labware, wavelength_nm: int, wells: list[str], progress: Progress) -> dict[str, float]:
        return await self._per_well(wells, progress, lambda w: round(0.04 + _unit(plate.id, w, wavelength_nm) * 2.5, 4))

    async def read_absorbance_spectrum(
        self, *, plate: Labware, wavelengths_nm: list[int], wells: list[str], progress: Progress
    ) -> dict[str, list[float]]:
        def spectrum(well: str) -> list[float]:  # one smooth peak per well
            peak, height = 350 + _unit(plate.id, well, "peak") * 400, 0.2 + _unit(plate.id, well, "height") * 2.0
            return [round(0.04 + height * 2.0 ** (-((nm - peak) / 60) ** 2), 4) for nm in wavelengths_nm]

        return await self._per_well(wells, progress, spectrum)

    async def read_fluorescence(
        self, *, plate: Labware, settings: FluorescenceSettings, wells: list[str], progress: Progress
    ) -> tuple[dict[str, float], int | None]:
        gain = settings.gain if settings.gain is not None else 100
        values = await self._per_well(wells, progress, lambda w: round(
            50 + _unit(plate.id, w, settings.excitation_nm, settings.emission_nm) * 40000 * gain / 100, 1))
        return values, gain

    async def read_luminescence(
        self, *, plate: Labware, integration_s: float, focal_height_mm: float | None, wells: list[str], progress: Progress
    ) -> dict[str, float]:
        return await self._per_well(wells, progress, lambda w: round(20 + _unit(plate.id, w, "lum") * 1e6 * integration_s))

    # -- incubation and shaking ----------------------------------------------------------------------

    def _settle(self) -> None:
        now = asyncio.get_running_loop().time()
        goal = self._target if self._target is not None else 22.0
        step = self.heat_c_per_s * (now - self._since)
        self._temperature = goal if abs(goal - self._temperature) <= step else self._temperature + step * (1 if goal > self._temperature else -1)
        self._since = now

    async def set_temperature(self, celsius: float) -> None:
        self._settle()
        self._target = celsius

    async def deactivate_temperature(self) -> None:
        self._settle()
        self._target = None

    async def get_temperature(self) -> tuple[float | None, float | None]:
        if self.spec.temperature_c is None:
            return None, None
        self._settle()
        return round(self._temperature, 2), self._target

    async def shake(self, *, mode: ShakeMode, rpm: int | None, duration_s: float) -> None:
        await asyncio.sleep(duration_s)
