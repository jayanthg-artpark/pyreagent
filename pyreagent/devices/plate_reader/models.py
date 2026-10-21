from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AbsorbanceRead(BaseModel):
    plate: str = Field(description="Labware id of the plate that was read.")
    wavelength_nm: int
    values: dict[str, float] = Field(description="Optical density (absorbance units) per well, e.g. {'A1': 0.052}.")
    read_at: datetime


class PlateReaderStatus(BaseModel):
    tray_open: bool
