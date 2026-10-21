from __future__ import annotations

from pydantic import BaseModel, Field


class Weight(BaseModel):
    grams: float = Field(description="Net weight in grams (gross load minus zero point and tare).")
    stable: bool = Field(description="True if the balance reported the reading as settled.")


class TareResult(BaseModel):
    tare_g: float = Field(description="Weight stored as tare, in grams. Later readings are net of it.")
