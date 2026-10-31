"""Tecan plate washers."""

from .hydroflex import TecanHydroFlexBackend

DRIVER = TecanHydroFlexBackend

__all__ = ["DRIVER", "TecanHydroFlexBackend"]
