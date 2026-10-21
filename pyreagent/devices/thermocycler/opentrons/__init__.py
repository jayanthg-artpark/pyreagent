"""Opentrons Thermocycler Module.

    robot.py  through the OT-2/Flex it is plugged into (default; shares the robot's run with the pipettes)
    usb.py    directly over USB serial, no robot needed: driver = "opentrons.usb:OpentronsThermocyclerUSBBackend"
"""

from .robot import OpentronsThermocyclerBackend
from .usb import OpentronsThermocyclerUSBBackend

DRIVER = OpentronsThermocyclerBackend

__all__ = ["DRIVER", "OpentronsThermocyclerBackend", "OpentronsThermocyclerUSBBackend"]
