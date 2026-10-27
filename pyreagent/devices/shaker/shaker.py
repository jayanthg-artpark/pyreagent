"""Shaker capability interface. Heating and a labware latch are optional features of the driver."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from pydantic import Field

from ...core.capability import Within, capability
from ...core.device import Device
from ...core.errors import DeviceTimeout, InvalidArgument, NeedsAttention
from .backend import ShakerBackend
from .models import ShakerStatus

Rpm = Annotated[float, Field(gt=0, description="Speed in rpm, within limits.min_rpm .. limits.max_rpm."), Within("min_rpm", "max_rpm")]
FEATURE_CAPABILITIES = {
    "set_temperature": "heating", "wait_for_temperature": "heating", "deactivate_heater": "heating",
    "open_latch": "latch", "close_latch": "latch",
}


class Shaker(Device[ShakerBackend]):
    """Orbital or plate shaker, optionally with a heater and a labware latch."""

    device_type = "shaker"

    def __init__(
        self, name: str, backend: ShakerBackend, *, min_rpm: float | None = None, max_rpm: float | None = None,
        poll_interval_s: float = 2.0,
    ) -> None:
        super().__init__(name, backend)
        low, high = backend.rpm_range or (None, None)
        self.min_rpm = min_rpm if min_rpm is not None else low
        self.max_rpm = max_rpm if max_rpm is not None else high
        if self.min_rpm is None or self.max_rpm is None:
            raise ValueError("give min_rpm and max_rpm (this driver does not know its speed range)")
        self.poll_interval_s = poll_interval_s

    def supports(self, capability_name: str) -> bool:
        feature = FEATURE_CAPABILITIES.get(capability_name)
        return feature is None or feature in self.backend.features

    def limits(self) -> dict[str, Any]:
        limits: dict[str, Any] = {"min_rpm": self.min_rpm, "max_rpm": self.max_rpm, "features": sorted(self.backend.features)}
        if "heating" in self.backend.features and self.backend.temperature_range_c:
            limits |= {"min_temperature_c": self.backend.temperature_range_c[0], "max_temperature_c": self.backend.temperature_range_c[1]}
        return limits

    async def _ready_to_shake(self) -> None:
        if "latch" in self.backend.features and (await self.backend.status()).latch != "closed":
            raise NeedsAttention("The labware latch is not closed.", recoverable=True,
                                 suggested_action="Make sure the plate is seated, call close_latch, then retry.")

    @capability(long_running=True)
    async def shake(
        self,
        rpm: Rpm,
        duration_s: Annotated[float, Field(gt=0, le=24 * 3600, description="How long to shake, in seconds.")],
    ) -> ShakerStatus:
        """Shake at a speed for a fixed time, then stop. Runs as a job; cancelling it stops the shaker."""
        await self._ready_to_shake()
        await self.backend.start(rpm)
        loop = asyncio.get_running_loop()
        end = loop.time() + duration_s
        try:
            while (remaining := end - loop.time()) > 0:
                self.report_progress(1 - remaining / duration_s, f"{remaining:.0f}s left at {rpm:g} rpm")
                await asyncio.sleep(min(1.0, remaining))
        finally:
            await self.backend.stop()
        return await self.backend.status()

    @capability
    async def start_shaking(self, rpm: Rpm) -> ShakerStatus:
        """Start shaking and keep going until stop_shaking. Prefer shake when the duration is known."""
        await self._ready_to_shake()
        await self.backend.start(rpm)
        return await self.backend.status()

    @capability(interrupt=True)
    async def stop_shaking(self) -> ShakerStatus:
        """Stop shaking now. Safe at any time."""
        await self.backend.stop()
        return await self.backend.status()

    # -- heating ------------------------------------------------------------------------------------

    @capability
    async def set_temperature(
        self,
        celsius: Annotated[float, Field(ge=0, le=110, description="Heater set point, °C, within limits."),
                           Within("min_temperature_c", "max_temperature_c")],
    ) -> ShakerStatus:
        """Start heating to a set point and return at once. Use wait_for_temperature to wait."""
        await self.backend.set_temperature(celsius)
        return await self.backend.status()

    @capability(long_running=True)
    async def wait_for_temperature(
        self,
        tolerance_c: Annotated[float, Field(gt=0, le=10, description="Done when within this many °C of the set point.")] = 1.0,
        timeout_s: Annotated[float, Field(gt=0, le=4 * 3600, description="Give up with a 'timeout' error after this long.")] = 1800,
    ) -> ShakerStatus:
        """Wait until the heater reaches its set point. Runs as a job."""
        status = await self.backend.status()
        target = status.target_temperature_c
        if target is None or status.temperature_c is None:
            raise InvalidArgument("The heater is off; call set_temperature first.")
        start_gap = abs(status.temperature_c - target) or tolerance_c
        deadline = asyncio.get_running_loop().time() + timeout_s
        while abs(status.temperature_c - target) > tolerance_c:
            if asyncio.get_running_loop().time() > deadline:
                raise DeviceTimeout(f"Still at {status.temperature_c:.1f} °C after {timeout_s:g}s (set point {target:g} °C).")
            self.report_progress(max(0.0, 1 - abs(status.temperature_c - target) / start_gap), f"{status.temperature_c:.1f} °C -> {target:g} °C")
            await asyncio.sleep(self.poll_interval_s)
            status = await self.backend.status()
            if status.target_temperature_c != target or status.temperature_c is None:
                raise InvalidArgument("The set point changed while waiting.", recoverable=True)
        return status

    @capability(interrupt=True)
    async def deactivate_heater(self) -> ShakerStatus:
        """Turn the heater off. Safe at any time."""
        await self.backend.deactivate_heater()
        return await self.backend.status()

    # -- labware latch -------------------------------------------------------------------------------

    @capability
    async def open_latch(self) -> ShakerStatus:
        """Open the labware latch so a plate can be put on or taken off. Refused while shaking."""
        if (await self.backend.status()).shaking:
            raise InvalidArgument("The shaker is running; call stop_shaking first.", recoverable=True)
        await self.backend.open_latch()
        return await self.backend.status()

    @capability
    async def close_latch(self) -> ShakerStatus:
        """Close the labware latch to hold the plate. Required before shaking."""
        await self.backend.close_latch()
        return await self.backend.status()

    @capability(read_only=True)
    async def status(self) -> ShakerStatus:
        """Whether the shaker is running and at what speed; the heater and latch too, if it has them."""
        return await self.backend.status()
