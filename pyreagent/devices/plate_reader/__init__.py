"""Plate readers. Capability interface: ``PlateReader``. Drivers implement ``PlateReaderBackend``.

Brands:
    simulated/   deterministic simulated reader
"""

from .backend import PlateReaderBackend
from .models import AbsorbanceRead, PlateReaderStatus
from .plate_reader import PlateReader

__all__ = ["AbsorbanceRead", "PlateReader", "PlateReaderBackend", "PlateReaderStatus"]
