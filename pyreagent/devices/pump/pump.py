"""Pump capability interface."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from pydantic import Field

from ...core.capability import Within, capability
from ...core.device import Device
from ...core.errors import DeviceTimeout, HardwareError, Interrupted
from .backend import PumpBackend
from .models import PumpDirection, PumpRun, PumpStatus

VolumeMl = Annotated[
    float, Field(gt=0, description="Volume in millilitres. Must not exceed limits.max_volume_ml."), Within(high="max_volume_ml")
]
RateMlMin = Annotated[
    float,
    Field(gt=0, description="Flow rate in mL/min, within limits.min_rate_ml_min .. limits.max_rate_ml_min."),
    Within("min_rate_ml_min", "max_rate_ml_min"),
]


class Pump(Device[PumpBackend]):
    """Liquid pump (syringe or peristaltic) that moves a set volume at a set rate."""

    device_type = "pump"

    def __init__(
        self,
        name: str,
        backend: PumpBackend,
        *,
        max_volume_ml: float,
        max_rate_ml_min: float,
        min_rate_ml_min: float = 0.0,
        volume_tolerance: float = 0.01,
        poll_interval_s: float = 0.25,
    ) -> None:
        """
        Args:
            max_volume_ml: Largest single move; for a syringe pump, the syringe volume.
            max_rate_ml_min, min_rate_ml_min: Allowed flow-rate range for the fitted syringe/tubing.
            volume_tolerance: A move that ends more than this fraction short of the request is a fault.
        """
        super().__init__(name, backend)
        self.max_volume_ml = max_volume_ml
        self.max_rate_ml_min = max_rate_ml_min
        self.min_rate_ml_min = min_rate_ml_min
        self.volume_tolerance = volume_tolerance
        self.poll_interval_s = poll_interval_s
        self._stop_requested = False

    def limits(self) -> dict[str, Any]:
        return {
            "max_volume_ml": self.max_volume_ml,
            "min_rate_ml_min": self.min_rate_ml_min,
            "max_rate_ml_min": self.max_rate_ml_min,
        }

    @capability(long_running=True)
    async def dispense(self, volume_ml: VolumeMl, rate_ml_min: RateMlMin) -> PumpRun:
        """Dispense (infuse) a volume at a flow rate.

        Runs as a job; poll it for progress. Fails with 'interrupted' if stop is
        called first, and with a hardware error if the pump stalls or stops short.
        """
        return await self._move(PumpDirection.INFUSE, volume_ml, rate_ml_min)

    @capability(long_running=True)
    async def withdraw(self, volume_ml: VolumeMl, rate_ml_min: RateMlMin) -> PumpRun:
        """Withdraw (aspirate) a volume at a flow rate. Runs as a job, like dispense."""
        return await self._move(PumpDirection.WITHDRAW, volume_ml, rate_ml_min)

    @capability(interrupt=True)
    async def stop(self) -> PumpStatus:
        """Stop the pump now. Safe at any time; a running dispense/withdraw job then fails with 'interrupted'."""
        self._stop_requested = True
        await self.backend.stop()
        return await self.backend.status()

    @capability(read_only=True)
    async def status(self) -> PumpStatus:
        """Whether the pump is running and how much it has moved since the last dispense/withdraw started."""
        return await self.backend.status()

    async def on_cancel(self, capability_name: str) -> None:
        if capability_name in ("dispense", "withdraw"):
            await self.backend.stop()

    async def _move(self, direction: PumpDirection, volume_ml: float, rate_ml_min: float) -> PumpRun:
        loop = asyncio.get_running_loop()
        expected_s = volume_ml / rate_ml_min * 60
        started = loop.time()
        deadline = started + expected_s * 1.5 + 10
        self._stop_requested = False
        await self.backend.start(direction=direction, volume_ml=volume_ml, rate_ml_min=rate_ml_min)

        moved = 0.0
        while True:
            await asyncio.sleep(self.poll_interval_s)
            status = await self.backend.status()
            moved = status.infused_ml if direction is PumpDirection.INFUSE else status.withdrawn_ml
            self.report_progress(moved / volume_ml, f"{moved:.4g} of {volume_ml:g} mL")
            if status.alarm:
                raise HardwareError(f"Pump alarm: {status.alarm}", details={"moved_ml": moved, "requested_ml": volume_ml})
            if not status.running:
                break
            if loop.time() > deadline:
                await self.backend.stop()
                raise DeviceTimeout(
                    f"Pump still running after {loop.time() - started:.0f}s (expected {expected_s:.0f}s); stopped it.",
                    faults_device=True,
                    details={"moved_ml": moved, "requested_ml": volume_ml},
                )

        details = {"moved_ml": moved, "requested_ml": volume_ml}
        if self._stop_requested:
            raise Interrupted(f"Stopped after {moved:.4g} of {volume_ml:g} mL.", details=details)
        if moved < volume_ml * (1 - self.volume_tolerance):
            raise HardwareError(f"Pump stopped early: moved {moved:.4g} of {volume_ml:g} mL.", details=details)
        return PumpRun(direction=direction, requested_ml=volume_ml, moved_ml=moved, rate_ml_min=rate_ml_min,
                       duration_s=round(loop.time() - started, 3))
