from __future__ import annotations

import asyncio
import hashlib
from typing import Callable

from ....labware import Labware
from ..backend import PlateReaderBackend


class SimulatedPlateReaderBackend(PlateReaderBackend):
    """Returns repeatable pseudo-random ODs, so agent plans can be checked end to end."""

    vendor = "pyreagent"
    model = "Simulated plate reader"
    simulated = True

    def __init__(self, *, seconds_per_well: float = 0.01, tray_s: float = 0.05) -> None:
        self.seconds_per_well = seconds_per_well
        self.tray_s = tray_s
        self._tray_open = False

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def open_tray(self) -> None:
        await asyncio.sleep(self.tray_s)
        self._tray_open = True

    async def close_tray(self) -> None:
        await asyncio.sleep(self.tray_s)
        self._tray_open = False

    async def tray_is_open(self) -> bool:
        return self._tray_open

    @staticmethod
    def _od(plate: str, well: str, wavelength_nm: int) -> float:
        digest = hashlib.sha256(f"{plate}/{well}/{wavelength_nm}".encode()).digest()
        return round(0.04 + int.from_bytes(digest[:4], "big") / 2**32 * 2.5, 4)

    async def read_absorbance(
        self, *, plate: Labware, wavelength_nm: int, wells: list[str], progress: Callable[[float], None]
    ) -> dict[str, float]:
        values: dict[str, float] = {}
        for index, well in enumerate(wells, start=1):
            if self.seconds_per_well:
                await asyncio.sleep(self.seconds_per_well)
            values[well] = self._od(plate.id, well, wavelength_nm)
            progress(index / len(wells))
        return values
