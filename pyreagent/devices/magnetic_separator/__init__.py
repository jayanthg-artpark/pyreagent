"""Magnetic separators. Capability interface: ``MagneticSeparator``. Drivers implement ``MagneticSeparatorBackend``.

Brands:
    simulated/   in-memory magnet
    opentrons/   Opentrons Magnetic Module GEN1/GEN2, through the OT-2
"""

from .backend import MagneticSeparatorBackend
from .magnetic_separator import MagneticSeparator
from .models import MagnetStatus

__all__ = ["MagnetStatus", "MagneticSeparator", "MagneticSeparatorBackend"]
