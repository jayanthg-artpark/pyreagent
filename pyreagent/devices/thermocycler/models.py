from __future__ import annotations

from pydantic import BaseModel, Field


class ThermalStep(BaseModel):
    temperature_c: float = Field(description="Block temperature for this step, °C.")
    hold_s: float = Field(gt=0, le=24 * 3600, description="Hold time once the block reaches temperature, seconds.")


class ThermalStage(BaseModel):
    steps: list[ThermalStep] = Field(min_length=1, max_length=10, description="Steps run in order, once per cycle.")
    repeat: int = Field(default=1, ge=1, le=200, description="Number of cycles of this stage (1 for an initial denaturation).")


class ThermalProfile(BaseModel):
    """A PCR program, e.g. stages [95 °C 180 s] x1, [95 °C 15 s, 60 °C 30 s] x35, [72 °C 300 s] x1."""

    stages: list[ThermalStage] = Field(min_length=1, max_length=20)
    lid_temperature_c: float = Field(default=105, ge=30, le=120, description="Heated lid setpoint, °C (stops condensation).")
    final_hold_c: float | None = Field(
        default=4.0, description="Hold the block at this temperature after the program; null to switch heating off."
    )

    @property
    def total_cycles(self) -> int:
        return sum(stage.repeat for stage in self.stages)

    @property
    def total_steps(self) -> int:
        return sum(stage.repeat * len(stage.steps) for stage in self.stages)


class ThermocyclerStatus(BaseModel):
    block_c: float
    block_target_c: float | None = Field(description="Active block setpoint, or null when block control is off.")
    lid_c: float
    lid_target_c: float | None
    lid_open: bool


class ProfileRun(BaseModel):
    total_cycles: int
    steps_run: int
    duration_s: float
    holding_at_c: float | None = Field(description="Block temperature now being held, or null if heating was switched off.")
