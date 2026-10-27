"""Plate readers. Capability interface: ``PlateReader``. Drivers implement ``PlateReaderBackend``.

Brands:
    simulated/           deterministic simulated multimode reader (or any narrower ReaderSpec)
    bmg_labtech/         BMG Labtech CLARIOstar / CLARIOstar Plus (FTDI serial)
    biotek/              Agilent BioTek Synergy H1, Cytation 1/5 and Synergy HT (serial)
    molecular_devices/   Molecular Devices SpectraMax M5, 384 Plus, Gemini EM (RS-232)
    tecan/               Tecan Infinite 200 PRO, M series (USB bulk)
    byonoy/              Byonoy Absorbance 96 / Luminescence 96 (USB HID)
"""

from .backend import FluorescenceSettings, PlateReaderBackend
from .models import (
    AbsorbanceRead,
    AbsorbanceSpec,
    AbsorbanceSpectrum,
    FluorescenceRead,
    FluorescenceSpec,
    LuminescenceRead,
    LuminescenceSpec,
    PlateReaderStatus,
    ReaderSpec,
    ShakeSpec,
)
from .plate_reader import PlateReader

__all__ = [
    "AbsorbanceRead", "AbsorbanceSpec", "AbsorbanceSpectrum", "FluorescenceRead", "FluorescenceSettings", "FluorescenceSpec",
    "LuminescenceRead", "LuminescenceSpec", "PlateReader", "PlateReaderBackend", "PlateReaderStatus", "ReaderSpec", "ShakeSpec",
]
