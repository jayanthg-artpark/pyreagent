"""Plate washers. Capability interface: ``PlateWasher``. Drivers implement ``PlateWasherBackend``.

Brands:
    simulated/   tracks buffer and waste volumes
"""

from .backend import PlateWasherBackend
from .models import PlateWasherStatus, WasherRun
from .plate_washer import PlateWasher

__all__ = ["PlateWasher", "PlateWasherBackend", "PlateWasherStatus", "WasherRun"]
