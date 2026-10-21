from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class PumpDirection(StrEnum):
    INFUSE = "infuse"
    WITHDRAW = "withdraw"


class PumpStatus(BaseModel):
    running: bool
    direction: PumpDirection | None = Field(description="Direction of the current or last move.")
    infused_ml: float = Field(description="Volume infused since the last move started, in mL.")
    withdrawn_ml: float = Field(description="Volume withdrawn since the last move started, in mL.")
    alarm: str | None = Field(default=None, description="Active pump alarm (stall, reset...), if any.")


class PumpRun(BaseModel):
    direction: PumpDirection
    requested_ml: float
    moved_ml: float = Field(description="Volume the pump reports it actually moved.")
    rate_ml_min: float
    duration_s: float
