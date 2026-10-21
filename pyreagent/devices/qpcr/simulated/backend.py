from __future__ import annotations

import hashlib
import math
import random

from ....labware import Labware
from ...thermocycler.backend import Progress
from ...thermocycler.simulated.backend import SimulatedThermocyclerBackend
from ..backend import QpcrBackend
from ..models import QpcrProfile


class SimulatedQpcrBackend(SimulatedThermocyclerBackend, QpcrBackend):
    """Sigmoid amplification curves. Each well/channel either amplifies (curve midpoint at cycle 18-34) or stays flat, repeatably."""

    model = "Simulated qPCR"

    def __init__(self, *, channels: list[str] | None = None, seed: int = 0, noise: float = 5.0, **thermal: object) -> None:
        super().__init__(**thermal)  # type: ignore[arg-type]
        self.channels = list(channels or ["FAM", "HEX", "ROX", "Cy5"])
        self.seed = seed
        self.noise = noise
        self.slope = 1.6
        self._cycle = 0

    def midpoint_cycle(self, well: str, channel: str) -> float | None:
        """Cycle at half-maximum signal, or None for a well/channel that does not amplify."""
        digest = hashlib.sha256(f"{self.seed}/{well}/{channel}".encode()).digest()
        if digest[0] % 5 == 0:
            return None
        return 18 + int.from_bytes(digest[1:3], "big") / 65535 * 16

    async def run_qpcr(self, *, profile: QpcrProfile, plate: Labware, wells: list[str], channels: list[str],
                       progress: Progress) -> dict[str, dict[str, list[float]]]:
        self._cycle = 0
        return await super().run_qpcr(profile=profile, plate=plate, wells=wells, channels=channels, progress=progress)

    async def read_fluorescence(self, *, wells: list[str], channels: list[str]) -> dict[str, dict[str, float]]:
        self._cycle += 1
        reading: dict[str, dict[str, float]] = {}
        for well in wells:
            reading[well] = {}
            for channel in channels:
                midpoint = self.midpoint_cycle(well, channel)
                rng = random.Random(f"{self.seed}/{well}/{channel}/{self._cycle}")
                signal = 0.0 if midpoint is None else 20000 / (1 + math.exp(-(self._cycle - midpoint) / self.slope))
                reading[well][channel] = 1000 + signal + rng.gauss(0, self.noise)
        return reading
