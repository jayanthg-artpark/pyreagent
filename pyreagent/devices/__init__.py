"""Device types, each with one folder per brand of driver.

    devices/<type>/<type>.py        capability interface (what agents see)
    devices/<type>/backend.py       driver contract for that type
    devices/<type>/models.py        data returned by capabilities
    devices/<type>/<brand>/         drivers for one manufacturer; exports DRIVER
    devices/<type>/simulated/       simulated driver every type must have

A config entry names a driver either by brand folder (``"mettler_toledo"``,
which uses that folder's ``DRIVER``), by ``"brand.module:Class"`` inside the
type, or by a full import path ``"my_lab.drivers:MyBackend"`` for drivers that
live outside pyreagent.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

from ..core.backend import Backend
from ..core.device import Device

DEVICE_TYPES: dict[str, str] = {
    "balance": "Balance",
    "pump": "Pump",
    "plate_reader": "PlateReader",
    "temperature_controller": "TemperatureController",
    "shaker": "Shaker",
    "liquid_handler": "LiquidHandler",
    "thermocycler": "Thermocycler",
    "qpcr": "Qpcr",
    "plate_washer": "PlateWasher",
    "magnetic_separator": "MagneticSeparator",
}


def device_class(device_type: str) -> type[Device[Any]]:
    try:
        class_name = DEVICE_TYPES[device_type]
    except KeyError:
        raise ValueError(f"unknown device type {device_type!r}; known: {sorted(DEVICE_TYPES)}") from None
    module = importlib.import_module(f"{__name__}.{device_type}")
    return getattr(module, class_name)


def resolve_driver(device_type: str, driver: str) -> type[Backend]:
    device_class(device_type)  # validates the type
    module_path, _, attr = driver.partition(":")
    candidates = [f"{__name__}.{device_type}.{module_path}", module_path]
    for candidate in candidates:
        try:
            module = importlib.import_module(candidate)
        except ModuleNotFoundError as exc:
            if exc.name and candidate.startswith(exc.name):
                continue
            raise
        cls = getattr(module, attr or "DRIVER", None)
        if cls is None:
            raise ValueError(f"{candidate} has no {'attribute ' + attr if attr else 'DRIVER export'}")
        if not (isinstance(cls, type) and issubclass(cls, Backend)):
            raise TypeError(f"{candidate}:{attr or 'DRIVER'} is not a Backend subclass")
        return cls
    raise ValueError(f"no driver {driver!r} for {device_type}; available brands: {available_drivers().get(device_type, [])}")


def available_drivers() -> dict[str, list[str]]:
    """Brand folders per device type."""
    result: dict[str, list[str]] = {}
    for device_type in DEVICE_TYPES:
        package = importlib.import_module(f"{__name__}.{device_type}")
        result[device_type] = sorted(m.name for m in pkgutil.iter_modules(package.__path__) if m.ispkg)
    return result


__all__ = ["DEVICE_TYPES", "available_drivers", "device_class", "resolve_driver"]
