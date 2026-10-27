from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Optics = Literal["top", "bottom"]
ShakeMode = Literal["orbital", "linear", "double_orbital"]


# -- what an instrument can do (declared by each driver) --------------------------------------------

class AbsorbanceSpec(BaseModel):
    min_nm: int
    max_nm: int
    spectrum: bool = Field(default=False, description="Can scan a wavelength range in one read.")
    max_spectrum_points: int = Field(default=0, description="Most wavelengths one spectrum read may cover.")
    fixed_nm: list[int] | None = Field(default=None, description="Filter instruments: the only wavelengths available.")


class FluorescenceSpec(BaseModel):
    min_excitation_nm: int
    max_excitation_nm: int
    min_emission_nm: int
    max_emission_nm: int
    min_stokes_shift_nm: int = Field(default=0, description="Emission must be at least this far above excitation.")
    min_gain: int | None = None
    max_gain: int | None = Field(default=None, description="None: the instrument picks its own gain.")
    optics: list[Optics] = ["top"]
    focal_height_mm: tuple[float, float] | None = Field(default=None, description="Settable read height range, mm.")
    max_flashes: int | None = None


class LuminescenceSpec(BaseModel):
    min_integration_s: float
    max_integration_s: float
    focal_height_mm: tuple[float, float] | None = None


class ShakeSpec(BaseModel):
    modes: list[ShakeMode]
    min_rpm: int | None = Field(default=None, description="None: speed is fixed or set by mode only.")
    max_rpm: int | None = None
    max_duration_s: float = 3600


class ReaderSpec(BaseModel):
    """Everything an instrument can do. A capability whose part is ``None`` is not offered."""

    absorbance: AbsorbanceSpec | None = None
    fluorescence: FluorescenceSpec | None = None
    luminescence: LuminescenceSpec | None = None
    temperature_c: tuple[float, float] | None = Field(default=None, description="Incubation set-point range.")
    shaking: ShakeSpec | None = None
    tray: bool = Field(default=True, description="Has a tray/drawer that opens and closes.")
    plate_formats: list[tuple[int, int]] | None = Field(default=None, description="(rows, columns) it reads; None: any.")
    needs_geometry: bool = Field(default=False, description="Plates need physical geometry (well positions) to be read.")


# -- results ----------------------------------------------------------------------------------------

class AbsorbanceRead(BaseModel):
    plate: str = Field(description="Labware id of the plate that was read.")
    wavelength_nm: int
    values: dict[str, float] = Field(description="Optical density (absorbance units) per well, e.g. {'A1': 0.052}.")
    read_at: datetime


class AbsorbanceSpectrum(BaseModel):
    plate: str
    wavelengths_nm: list[int]
    values: dict[str, list[float]] = Field(description="Per well, OD at each of wavelengths_nm, in order.")
    read_at: datetime


class FluorescenceRead(BaseModel):
    plate: str
    excitation_nm: int
    emission_nm: int
    optics: Optics
    gain: int | None = Field(description="Gain used; None if the instrument chose it and did not report it.")
    values: dict[str, float] = Field(description="Relative fluorescence units (RFU) per well. Only comparable at the same gain.")
    read_at: datetime


class LuminescenceRead(BaseModel):
    plate: str
    integration_s: float
    values: dict[str, float] = Field(description="Relative light units (RLU) per well.")
    read_at: datetime


class PlateReaderStatus(BaseModel):
    tray_open: bool | None = Field(default=None, description="None if the reader has no tray.")
    temperature_c: float | None = Field(default=None, description="Current chamber temperature, if the reader measures it.")
    target_temperature_c: float | None = Field(default=None, description="Incubation set point; None when off.")
