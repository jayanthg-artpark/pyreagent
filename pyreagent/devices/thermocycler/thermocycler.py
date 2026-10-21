"""Thermocycler (PCR) capability interface."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from pydantic import Field

from ...core.capability import Within, capability
from ...core.device import Device
from ...core.errors import InvalidArgument, NeedsAttention, NotSupported
from .backend import ThermocyclerBackend
from .models import ProfileRun, ThermalProfile, ThermocyclerStatus

_PROGRAMS = ("run_profile", "run_qpcr")


class Thermocycler(Device[ThermocyclerBackend]):
    """PCR thermocycler: a temperature-cycled block with a heated lid."""

    device_type = "thermocycler"

    def __init__(
        self,
        name: str,
        backend: ThermocyclerBackend,
        *,
        min_c: float = 4.0,
        max_c: float = 99.0,
        max_lid_c: float = 110.0,
        max_cycles: int = 100,
    ) -> None:
        super().__init__(name, backend)
        self.min_c, self.max_c, self.max_lid_c, self.max_cycles = min_c, max_c, max_lid_c, max_cycles

    def limits(self) -> dict[str, Any]:
        rows, columns = self.backend.block_format
        return {"min_c": self.min_c, "max_c": self.max_c, "max_lid_c": self.max_lid_c, "max_cycles": self.max_cycles,
                "block_rows": rows, "block_columns": columns}

    def _check_profile(self, profile: ThermalProfile) -> None:
        temperatures = [step.temperature_c for stage in profile.stages for step in stage.steps]
        if profile.final_hold_c is not None:
            temperatures.append(profile.final_hold_c)
        bad = sorted({t for t in temperatures if not self.min_c <= t <= self.max_c})
        if bad:
            raise InvalidArgument(f"Profile temperatures {bad} °C are outside {self.min_c}..{self.max_c} °C.",
                                  details={"min_c": self.min_c, "max_c": self.max_c})
        if profile.lid_temperature_c > self.max_lid_c:
            raise InvalidArgument(f"Lid temperature {profile.lid_temperature_c} °C exceeds {self.max_lid_c} °C.")
        if profile.total_cycles > self.max_cycles:
            raise InvalidArgument(f"{profile.total_cycles} cycles exceeds limits.max_cycles ({self.max_cycles}).")
        if "ramp_rate" not in self.backend.features and any(
            step.ramp_rate_c_per_s for stage in profile.stages for step in stage.steps
        ):
            raise NotSupported("This thermocycler cannot set ramp rates; remove ramp_rate_c_per_s from the steps.")

    def validate_arguments(self, capability_name: str, values: dict[str, Any]) -> None:
        if capability_name in _PROGRAMS:
            self._check_profile(values["profile"])
        if capability_name == "lift_plate" and "plate_lift" not in self.backend.features:
            raise NotSupported("This thermocycler has no plate lift.")

    async def _require_lid_closed(self) -> None:
        if await self.backend.lid_is_open():
            raise NeedsAttention("The lid is open.", recoverable=True,
                                 suggested_action="Check the plate is seated and sealed, call close_lid, then retry.")

    @capability(long_running=True)
    async def run_profile(self, profile: Annotated[ThermalProfile, Field(description="The PCR program to run.")]) -> ProfileRun:
        """Run a PCR program on the plate in the block. Runs as a job.

        The lid must be closed. When the program ends the block keeps holding
        final_hold_c (if set) until deactivate is called.
        """
        await self._require_lid_closed()
        loop = asyncio.get_running_loop()
        started = loop.time()
        steps = await self.backend.run_profile(profile, progress=self.report_progress)
        return ProfileRun(total_cycles=profile.total_cycles, steps_run=steps, duration_s=round(loop.time() - started, 3),
                          holding_at_c=profile.final_hold_c)

    @capability
    async def set_block_temperature(
        self,
        temperature_c: Annotated[float, Field(description="Block setpoint, °C, within limits.min_c .. limits.max_c."), Within("min_c", "max_c")],
    ) -> ThermocyclerStatus:
        """Hold the block at one temperature (e.g. to pre-chill before loading). Returns immediately."""
        await self.backend.set_block_temperature(temperature_c)
        return await self.backend.read()

    @capability
    async def open_lid(self) -> ThermocyclerStatus:
        """Open the lid to load or remove a plate."""
        await self.backend.open_lid()
        return await self.backend.read()

    @capability
    async def close_lid(self) -> ThermocyclerStatus:
        """Close the lid. The plate must be seated and sealed first."""
        await self.backend.close_lid()
        return await self.backend.read()

    @capability
    async def lift_plate(self) -> ThermocyclerStatus:
        """Raise the plate off the block so it can be removed (Opentrons Thermocycler GEN2). The lid must be open."""
        await self.backend.lift_plate()
        return await self.backend.read()

    @capability(read_only=True)
    async def status(self) -> ThermocyclerStatus:
        """Block and lid temperatures, setpoints, and whether the lid is open."""
        return await self.backend.read()

    @capability(interrupt=True)
    async def deactivate(self) -> ThermocyclerStatus:
        """Switch block and lid heating off. Safe at any time; a running program job is cancelled."""
        self.cancel_activity()
        await self.backend.deactivate()
        return await self.backend.read()

    async def on_cancel(self, capability_name: str) -> None:
        if capability_name in _PROGRAMS:
            await self.backend.deactivate()
