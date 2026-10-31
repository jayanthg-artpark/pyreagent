"""Avay Bioscience liquid handlers."""

from .liquidxpert import AvayLiquidXpertBackend

DRIVER = AvayLiquidXpertBackend

__all__ = ["AvayLiquidXpertBackend", "DRIVER"]
