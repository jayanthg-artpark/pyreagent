"""Contract every qPCR driver implements: a thermocycler plus fluorescence optics.

``run_qpcr`` has a default built on ``run_profile`` + ``read_fluorescence``.
Instruments that run qPCR programs natively and return curves at the end should override it.
"""

from __future__ import annotations

import abc
from typing import Any

from ...labware import Labware
from ..thermocycler.backend import Progress, ThermocyclerBackend
from ..thermocycler.models import ThermalStep
from .models import QpcrProfile


class QpcrBackend(ThermocyclerBackend):
    channels: list[str] = []
    """Detection channels / dyes the optics support, e.g. ["FAM", "HEX", "ROX", "Cy5"]."""

    @abc.abstractmethod
    async def read_fluorescence(self, *, wells: list[str], channels: list[str]) -> dict[str, dict[str, float]]:
        """One fluorescence value per well and channel."""

    async def run_qpcr(
        self, *, profile: QpcrProfile, plate: Labware, wells: list[str], channels: list[str], progress: Progress
    ) -> dict[str, dict[str, list[float]]]:
        curves: dict[str, dict[str, list[float]]] = {w: {c: [] for c in channels} for w in wells}

        async def acquire(step: ThermalStep) -> None:
            if getattr(step, "acquire", False):
                reading = await self.read_fluorescence(wells=wells, channels=channels)
                for w in wells:
                    for c in channels:
                        curves[w][c].append(reading[w][c])

        await self.run_profile(profile, progress=progress, after_step=acquire)
        return curves

    def info(self) -> dict[str, Any]:
        return {**super().info(), "channels": list(self.channels)}
