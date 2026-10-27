from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ShakerStatus(BaseModel):
    shaking: bool
    rpm: float = Field(description="Current speed in rpm (0 when stopped).")
    temperature_c: float | None = Field(default=None, description="Current plate temperature, if the shaker heats.")
    target_temperature_c: float | None = Field(default=None, description="Heater set point; None when the heater is off.")
    latch: Literal["open", "closed", "unknown"] | None = Field(
        default=None, description="Labware latch, if the shaker has one. It must be closed before shaking."
    )
