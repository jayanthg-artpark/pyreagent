"""Contract every thermocycler driver implements.

Drivers implement the primitives. ``run_profile`` has a default implementation
built on them (set block, wait to reach it, hold); a driver whose instrument
runs programs natively, with its own precise timing, should override it.
"""

from __future__ import annotations

import abc
import asyncio
from typing import Any, Awaitable, Callable, Literal

from ...core.backend import Backend
from ...core.errors import HardwareError
from .models import ThermalProfile, ThermalStep, ThermocyclerStatus

Progress = Callable[[float, str], None]
AfterStep = Callable[[ThermalStep], Awaitable[None]]


class ThermocyclerBackend(Backend):
    block_format: tuple[int, int] = (8, 12)
    """Rows and columns of the block."""
    poll_s: float = 0.5
    ramp_timeout_s: float = 600.0

    @abc.abstractmethod
    async def open_lid(self) -> None: ...

    @abc.abstractmethod
    async def close_lid(self) -> None: ...

    @abc.abstractmethod
    async def lid_is_open(self) -> bool: ...

    @abc.abstractmethod
    async def set_block_temperature(self, target_c: float) -> None: ...

    @abc.abstractmethod
    async def set_lid_temperature(self, target_c: float) -> None: ...

    @abc.abstractmethod
    async def deactivate_block(self) -> None: ...

    @abc.abstractmethod
    async def deactivate_lid(self) -> None: ...

    @abc.abstractmethod
    async def read(self) -> ThermocyclerStatus: ...

    async def deactivate(self) -> None:
        await self.deactivate_block()
        await self.deactivate_lid()

    async def hold(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def wait_for(self, part: Literal["block", "lid"], target_c: float, *, tolerance_c: float = 0.5) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ramp_timeout_s
        while True:
            status = await self.read()
            current = status.block_c if part == "block" else status.lid_c
            if abs(current - target_c) <= tolerance_c:
                return
            if loop.time() > deadline:
                raise HardwareError(f"{part} stuck at {current:.1f} °C, did not reach {target_c:g} °C within {self.ramp_timeout_s:g}s.")
            await asyncio.sleep(self.poll_s)

    async def run_profile(self, profile: ThermalProfile, *, progress: Progress, after_step: AfterStep | None = None) -> int:
        """Run the program; return the number of steps run. ``after_step`` runs at the end of each hold."""
        await self.set_lid_temperature(profile.lid_temperature_c)
        progress(0.0, f"heating lid to {profile.lid_temperature_c:g} °C")
        await self.wait_for("lid", profile.lid_temperature_c, tolerance_c=2.0)
        done, total = 0, profile.total_steps
        for s, stage in enumerate(profile.stages, start=1):
            for cycle in range(1, stage.repeat + 1):
                for step in stage.steps:
                    progress(done / total, f"stage {s}/{len(profile.stages)}, cycle {cycle}/{stage.repeat}: {step.temperature_c:g} °C")
                    await self.set_block_temperature(step.temperature_c)
                    await self.wait_for("block", step.temperature_c)
                    await self.hold(step.hold_s)
                    if after_step is not None:
                        await after_step(step)
                    done += 1
        if profile.final_hold_c is None:
            await self.deactivate()
        else:
            await self.set_block_temperature(profile.final_hold_c)
        return done

    def info(self) -> dict[str, Any]:
        return {**super().info(), "block_format": list(self.block_format)}
