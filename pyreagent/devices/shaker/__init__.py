"""Shakers. Capability interface: ``Shaker``. Drivers implement ``ShakerBackend``.

Brands:
    simulated/   in-memory shaker (optionally a heater-shaker)
    opentrons/   Opentrons Heater-Shaker Module, through the robot
"""

from .backend import ShakerBackend
from .models import ShakerStatus
from .shaker import Shaker

__all__ = ["Shaker", "ShakerBackend", "ShakerStatus"]
