from __future__ import annotations

import asyncio
import time
from typing import Literal

from ..backend import ThermocyclerBackend
from ..models import ThermocyclerStatus


class _Ramp:
    """Linear ramp towards the setpoint (ambient when off), on a time axis scaled by ``speedup``."""

    def __init__(self, ambient_c: float, rate_c_per_s: float, speedup: float) -> None:
        self.ambient_c, self.rate, self.speedup = ambient_c, rate_c_per_s, speedup
        self.current = ambient_c
        self.target: float | None = None
        self._t = time.monotonic()

    def advance(self) -> float:
        now = time.monotonic()
        goal = self.target if self.target is not None else self.ambient_c
        step = self.rate * (now - self._t) * self.speedup
        self._t = now
        if abs(goal - self.current) <= step:
            self.current = goal
        else:
            self.current += step if goal > self.current else -step
        return self.current

    def set(self, target: float | None) -> None:
        self.advance()
        self.target = target


class SimulatedThermocyclerBackend(ThermocyclerBackend):
    vendor = "pyreagent"
    model = "Simulated thermocycler"
    simulated = True

    def __init__(
        self,
        *,
        ramp_c_per_s: float = 4.0,
        lid_ramp_c_per_s: float = 1.0,
        ambient_c: float = 22.0,
        speedup: float = 1.0,
        block_format: tuple[int, int] = (8, 12),
        poll_s: float = 0.05,
    ) -> None:
        """``speedup`` compresses ramps and holds, e.g. 100 runs a 1 h program in 36 s."""
        self.speedup = speedup
        self.block_format = tuple(block_format)  # type: ignore[assignment]
        self.poll_s = poll_s
        self._block = _Ramp(ambient_c, ramp_c_per_s, speedup)
        self._lid = _Ramp(ambient_c, lid_ramp_c_per_s, speedup)
        self._lid_open = False

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        await self.deactivate()

    async def open_lid(self) -> None:
        self._lid_open = True

    async def close_lid(self) -> None:
        self._lid_open = False

    async def lid_is_open(self) -> bool:
        return self._lid_open

    async def set_block_temperature(self, target_c: float) -> None:
        self._block.set(target_c)

    async def set_lid_temperature(self, target_c: float) -> None:
        self._lid.set(target_c)

    async def deactivate_block(self) -> None:
        self._block.set(None)

    async def deactivate_lid(self) -> None:
        self._lid.set(None)

    @staticmethod
    async def _sleep(seconds: float) -> None:
        # Sub-millisecond sleeps would round up to the OS timer tick (~15 ms on Windows).
        await asyncio.sleep(seconds if seconds >= 0.002 else 0)

    async def hold(self, seconds: float) -> None:
        await self._sleep(seconds / self.speedup)

    async def wait_for(self, part: Literal["block", "lid"], target_c: float, *, tolerance_c: float = 0.5) -> None:
        ramp = self._block if part == "block" else self._lid
        if ramp.target != target_c:  # not being driven there: behave like real hardware (poll, then time out)
            return await super().wait_for(part, target_c, tolerance_c=tolerance_c)
        await self._sleep(abs(target_c - ramp.advance()) / (ramp.rate * self.speedup))
        ramp.advance()
        ramp.current = target_c

    async def read(self) -> ThermocyclerStatus:
        return ThermocyclerStatus(
            block_c=round(self._block.advance(), 2), block_target_c=self._block.target,
            lid_c=round(self._lid.advance(), 2), lid_target_c=self._lid.target, lid_open=self._lid_open,
        )
