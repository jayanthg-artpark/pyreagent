from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PlateWasherStatus(BaseModel):
    buffers_ml: dict[str, float | None] = Field(description="Remaining buffer per bottle in mL (null if the washer cannot tell).")
    waste_ml: float | None = Field(description="Liquid in the waste bottle, mL (null if unknown).")
    waste_capacity_ml: float | None
    waste_full: bool | None = Field(description="Whether the waste bottle is full (null if the washer cannot tell).")
    notes: dict[str, str] = Field(default_factory=dict, description="Other state the washer reports, e.g. firmware or rinse status.")


class WasherRun(BaseModel):
    operation: Literal["wash", "dispense", "prime"]
    plate: str | None
    buffer: str
    columns: list[int]
    cycles: int
    buffer_used_ml: float | None = Field(description="Buffer used, mL (null when priming by time).")
    duration_s: float


class WasherProgram(BaseModel):
    """A wash program stored on the washer."""

    number: int
    name: str
    plate: int | None = Field(default=None, description="Number of the washer's own plate definition the program is written for.")
    steps: list[str] = Field(default_factory=list, description="Steps as the washer reports them.")


class ProgramRun(BaseModel):
    program: int
    name: str
    phases: list[str] = Field(description="Phases the washer reported while running, in order.")
    duration_s: float
