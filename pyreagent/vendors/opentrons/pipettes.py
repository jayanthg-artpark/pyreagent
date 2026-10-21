"""Opentrons pipette facts: default flow rates (µL/s) per tip size, tip compatibility, low-volume mode.

Every Opentrons aspirate/dispense/blow-out command needs an explicit flowRate,
and the robot does not report the defaults, so they live here. Values are the
``default`` entries of Opentrons' pipette definitions
(``shared-data/pipette/definitions/2/liquid/<channels>/<model>/<liquidClass>/<version>.json``,
``supportedTips.tNN.default{Aspirate,Dispense,BlowOut}FlowRate``), Opentrons commit c098bd4
(2026-10-21). Keyed by the ``loadPipette`` name and tip volume (µL).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FlowRates:
    aspirate: float
    dispense: float
    blow_out: float


def _same(rate: float) -> FlowRates:
    return FlowRates(rate, rate, rate)


FLOW_RATES: dict[str, dict[int, FlowRates]] = {
    "p20_single_gen2": {10: _same(7.56), 20: _same(7.56)},
    "p20_multi_gen2": {10: _same(7.6), 20: _same(7.6)},
    "p300_single_gen2": {200: _same(92.86), 300: _same(92.86)},
    "p300_multi_gen2": {200: _same(94.0), 300: _same(94.0)},
    "p1000_single_gen2": {1000: _same(274.7)},
    "p50_single_flex": {20: FlowRates(22, 22, 57), 50: FlowRates(35, 57, 57)},
    "p50_multi_flex": {20: FlowRates(35, 57, 57), 50: FlowRates(35, 57, 57)},
    "p1000_single_flex": {50: _same(478), 200: _same(716), 1000: _same(716)},
    "p1000_multi_flex": {50: _same(478), 200: _same(716), 1000: _same(716)},
    "p1000_96": {50: FlowRates(6, 6, 80), 200: _same(80), 1000: FlowRates(160, 160, 80)},
}


@dataclass(frozen=True)
class LowVolumeMode:
    """Flex 50 µL pipettes switch liquid class below ``below_ul`` (``configureForVolume``)."""

    below_ul: float
    min_ul: float
    max_ul: float
    rates: dict[int, FlowRates]


LOW_VOLUME: dict[str, LowVolumeMode] = {
    "p50_single_flex": LowVolumeMode(5.0, 0.5, 30.0, {20: FlowRates(22, 22, 26.7), 50: _same(26.7)}),
    "p50_multi_flex": LowVolumeMode(5.0, 0.5, 30.0, {20: _same(26.7), 50: _same(32.6)}),
}


def tip_volumes(pipette_name: str) -> list[float] | None:
    """Tip sizes a pipette takes: from its flow-rate table, else from its name (GEN1 models)."""
    if pipette_name in FLOW_RATES:
        return [float(v) for v in sorted(FLOW_RATES[pipette_name])]
    if pipette_name.startswith("p1000"):
        return [1000.0]
    if pipette_name.startswith(("p50", "p300")):
        return [200.0, 300.0]
    if pipette_name.startswith(("p20", "p10")):
        return [10.0, 20.0] if pipette_name.startswith("p20") else [10.0]
    return None


def flow_rates(pipette_name: str, tip_volume_ul: float | None, *, low_volume: bool = False) -> FlowRates | None:
    """Default rates for this pipette with this tip (nearest larger tip size if not listed exactly)."""
    table = LOW_VOLUME[pipette_name].rates if low_volume and pipette_name in LOW_VOLUME else FLOW_RATES.get(pipette_name)
    if not table:
        return None
    sizes = sorted(table)
    if tip_volume_ul is None:
        return table[sizes[-1]]
    return table[next((s for s in sizes if s >= tip_volume_ul - 1e-6), sizes[-1])]
