"""Tecan EVO water liquid classes for disposable tips (DiTi).

Values from the EVOware-style classes in PyLabRobot (MIT): ``Water free DiTi`` style classes
selected by volume range [min, max). Speeds in µL/s, volumes in µL, distances in mm.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...core.errors import InvalidArgument


@dataclass(frozen=True)
class LiquidClass:
    min_ul: float
    max_ul: float
    calibration_factor: float
    calibration_offset: float
    aspirate_speed: float
    lag_volume: float
    lag_speed: float
    tag_volume: float
    tag_speed: float
    lld_offset_mm: float
    dispense_speed: float
    dispense_breakoff: float
    aspirate_lld: bool = True
    lld_mode: int = 7
    lld_conductivity: int = 1
    lld_speed_mm_s: float = 60.0
    lld_distance_mm: float = 4.0
    aspirate_retract_speed_mm_s: float = 20.0

    def corrected(self, volume_ul: float) -> float:
        return self.calibration_factor * volume_ul + self.calibration_offset


WATER_DITI = (
    LiquidClass(3, 15, 1.042, 1.1, 20, 5, 20, 10, 20, 2, 600, 400),
    LiquidClass(15, 200, 1.034, 0.9, 100, 5, 70, 10, 70, 2, 600, 400),
    LiquidClass(200, 1000, 1.031, 6.12, 150, 10, 70, 10, 70, 2, 600, 400),
)


def water_diti(volume_ul: float) -> LiquidClass:
    for lc in WATER_DITI:
        if lc.min_ul <= volume_ul < lc.max_ul:
            return lc
    raise InvalidArgument(f"No Tecan water liquid class for {volume_ul:g} µL (classes cover 3-1000 µL).")
