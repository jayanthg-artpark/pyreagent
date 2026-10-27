from __future__ import annotations

from pydantic import BaseModel, Field


class MagnetStatus(BaseModel):
    engaged: bool = Field(description="Whether the magnets are raised to the plate.")
    height_mm: float | None = Field(
        default=None, description="Height of the magnet tops above the surface the labware rests on, mm (when engaged)."
    )
