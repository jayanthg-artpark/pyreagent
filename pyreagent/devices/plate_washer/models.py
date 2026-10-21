from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PlateWasherStatus(BaseModel):
    buffers_ml: dict[str, float | None] = Field(description="Remaining buffer per bottle in mL (null if the washer cannot tell).")
    waste_ml: float | None = Field(description="Liquid in the waste bottle, mL (null if unknown).")
    waste_capacity_ml: float | None
    waste_full: bool


class WasherRun(BaseModel):
    operation: Literal["wash", "dispense", "prime"]
    plate: str | None
    buffer: str
    columns: list[int]
    cycles: int
    buffer_used_ml: float
    duration_s: float
