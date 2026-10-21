"""Shaker capability interface."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from pydantic import Field

from ...core.capability import Within, capability
from ...core.device import Device
from .backend import ShakerBackend
from .models import ShakerStatus

Rpm = Annotated[float, Field(gt=0, description="Speed in rpm, within limits.min_rpm .. limits.max_rpm."), Within("min_rpm", "max_rpm")]


class Shaker(Device[ShakerBackend]):
    """Orbital or plate shaker."""

    device_type = "shaker"

    def __init__(
        self, name: str, backend: ShakerBackend, *, min_rpm: float, max_rpm: float
    ) -> None:
        super().__init__(name, backend)
        self.min_rpm = min_rpm
        self.max_rpm = max_rpm

    def limits(self) -> dict[str, Any]:
        return {"min_rpm": self.min_rpm, "max_rpm": self.max_rpm}

    @capability(long_running=True)
    async def shake(
        self,
        rpm: Rpm,
        duration_s: Annotated[float, Field(gt=0, le=24 * 3600, description="How long to shake, in seconds.")],
    ) -> ShakerStatus:
        """Shake at a speed for a fixed time, then stop. Runs as a job; cancelling it stops the shaker."""
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
        await self.backend.start(rpm)
        return await self.backend.status()

    @capability(interrupt=True)
    async def stop_shaking(self) -> ShakerStatus:
        """Stop shaking now. Safe at any time."""
        await self.backend.stop()
        return await self.backend.status()

    @capability(read_only=True)
    async def status(self) -> ShakerStatus:
        """Whether the shaker is running and at what speed."""
        return await self.backend.status()
