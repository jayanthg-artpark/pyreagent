"""Opentrons Flex Absorbance Plate Reader Module, driven through the robot."""

from .robot import OpentronsAbsorbanceReaderBackend

DRIVER = OpentronsAbsorbanceReaderBackend

__all__ = ["DRIVER", "OpentronsAbsorbanceReaderBackend"]
