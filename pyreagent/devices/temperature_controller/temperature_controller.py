"""Temperature controller capability interface."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Mapping

from pydantic import Field

from ...core.capability import Within, capability
from ...core.device import Device
from ...core.errors import DeviceTimeout, InvalidArgument
from .backend import TemperatureControllerBackend
from .models import TemperatureReading


class TemperatureController(Device[TemperatureControllerBackend]):
    """Heating and/or cooling device with a setpoint (heat block, chiller, incubator, hot plate)."""

    device_type = "temperature_controller"

    def __init__(
        self,
        name: str,
        backend: TemperatureControllerBackend,
        *,
        min_c: float,
        max_c: float,
        poll_interval_s: float = 1.0,
        confirm: Mapping[str, bool] | None = None,
    ) -> None:
        """
        Args:
            min_c, max_c: Setpoints outside this range are refused (the safe range for
                this unit *and* the labware used on it, not just the hardware maximum).
        """
        super().__init__(name, backend, confirm=confirm)
        self.min_c = min_c
        self.max_c = max_c
        self.poll_interval_s = poll_interval_s

    def limits(self) -> dict[str, Any]:
        return {"min_c": self.min_c, "max_c": self.max_c}

    @capability(requires_confirmation=True, hazard="Turns on heating/cooling; the unit and its contents will get hot or cold.")
    async def set_temperature(
        self,
        target_c: Annotated[float, Field(description="Setpoint in °C, within limits.min_c .. limits.max_c."), Within("min_c", "max_c")],
    ) -> TemperatureReading:
        """Turn on temperature control and set the target. Returns immediately.

        Call wait_for_temperature afterwards if the next step needs the target reached.
        """
        await self.backend.set_target(target_c)
        return await self.backend.read()

    @capability(long_running=True)
    async def wait_for_temperature(
        self,
        tolerance_c: Annotated[float, Field(gt=0, le=10, description="Done when within this many °C of the target.")] = 0.5,
        timeout_s: Annotated[float, Field(gt=0, le=4 * 3600, description="Give up with a 'timeout' error after this long.")] = 1800,
    ) -> TemperatureReading:
        """Wait until the measured temperature reaches the current setpoint. Runs as a job."""
        reading = await self.backend.read()
        if not reading.active or reading.target_c is None:
            raise InvalidArgument("Temperature control is off; call set_temperature first.")
        target = reading.target_c
        start_gap = abs(reading.current_c - target) or tolerance_c
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while abs(reading.current_c - target) > tolerance_c:
            if loop.time() > deadline:
                raise DeviceTimeout(
                    f"Still at {reading.current_c:.2f} °C after {timeout_s:g}s (target {target:g} °C).",
                    suggested_action="Check that the unit is heating/cooling and the lid is closed, or allow more time.",
                    details=reading.model_dump(),
                )
            self.report_progress(1 - abs(reading.current_c - target) / start_gap, f"{reading.current_c:.2f} °C -> {target:g} °C")
            await asyncio.sleep(self.poll_interval_s)
            reading = await self.backend.read()
            if reading.target_c != target:
                raise InvalidArgument("The setpoint changed while waiting.", recoverable=True)
        return reading

    @capability(read_only=True)
    async def get_temperature(self) -> TemperatureReading:
        """Read the current temperature and setpoint."""
        return await self.backend.read()

    @capability(interrupt=True)
    async def deactivate(self) -> TemperatureReading:
        """Turn heating/cooling off. Safe at any time."""
        await self.backend.deactivate()
        return await self.backend.read()
