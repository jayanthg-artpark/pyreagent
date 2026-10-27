"""Opentrons Heater-Shaker Module, driven through the robot."""

from .robot import OpentronsHeaterShakerBackend

DRIVER = OpentronsHeaterShakerBackend

__all__ = ["DRIVER", "OpentronsHeaterShakerBackend"]
