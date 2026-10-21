"""Temperature controllers. Capability interface: ``TemperatureController``.
Drivers implement ``TemperatureControllerBackend``.

Brands:
    simulated/   first-order thermal model
"""

from .backend import TemperatureControllerBackend
from .models import TemperatureReading
from .temperature_controller import TemperatureController

__all__ = ["TemperatureController", "TemperatureControllerBackend", "TemperatureReading"]
