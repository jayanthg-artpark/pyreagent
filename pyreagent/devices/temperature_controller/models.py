from __future__ import annotations

from pydantic import BaseModel, Field


class TemperatureReading(BaseModel):
    current_c: float = Field(description="Measured temperature in °C.")
    target_c: float | None = Field(description="Active setpoint in °C, or null when control is off.")
    active: bool = Field(description="True while heating/cooling is on.")
