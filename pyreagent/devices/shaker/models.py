from __future__ import annotations

from pydantic import BaseModel, Field


class ShakerStatus(BaseModel):
    shaking: bool
    rpm: float = Field(description="Current speed in rpm (0 when stopped).")
