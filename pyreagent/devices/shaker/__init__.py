"""Shakers. Capability interface: ``Shaker``. Drivers implement ``ShakerBackend``.

Brands:
    simulated/   in-memory shaker
"""

from .backend import ShakerBackend
from .models import ShakerStatus
from .shaker import Shaker

__all__ = ["Shaker", "ShakerBackend", "ShakerStatus"]
