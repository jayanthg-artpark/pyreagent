from __future__ import annotations

from ..backend import ShakerBackend
from ..models import ShakerStatus


class SimulatedShakerBackend(ShakerBackend):
    vendor = "pyreagent"
    model = "Simulated shaker"
    simulated = True

    def __init__(self) -> None:
        self._rpm = 0.0

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        await self.stop()

    async def start(self, rpm: float) -> None:
        self._rpm = rpm

    async def stop(self) -> None:
        self._rpm = 0.0

    async def status(self) -> ShakerStatus:
        return ShakerStatus(shaking=self._rpm > 0, rpm=self._rpm)
