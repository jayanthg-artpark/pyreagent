"""Opentrons Temperature Module.

    robot.py  through the OT-2/Flex it is plugged into (default; shares the robot's run with the pipettes)
    usb.py    directly over USB serial, no robot needed: driver = "opentrons.usb:OpentronsTemperatureModuleUSBBackend"
"""

from .robot import OpentronsTemperatureModuleBackend
from .usb import OpentronsTemperatureModuleUSBBackend

DRIVER = OpentronsTemperatureModuleBackend

__all__ = ["DRIVER", "OpentronsTemperatureModuleBackend", "OpentronsTemperatureModuleUSBBackend"]
