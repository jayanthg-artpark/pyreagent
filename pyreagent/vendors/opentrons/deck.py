"""Opentrons Flex deck configuration: which cutouts hold slots, trash bins, staging areas, a waste chute.

From robot-server ``GET /deck_configuration`` (``cutoutFixtures``) and the
fixture definitions in ``shared-data/deck/definitions/5/ot3_standard.json``.
Staging-area slots (A4-D4) are addressed as ``{"addressableAreaName": "A4"}``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SLOT_FIXTURES = {"singleLeftSlot", "singleCenterSlot", "singleRightSlot"}
MODULE_FIXTURES = {"temperatureModuleV2", "heaterShakerModuleV1", "magneticBlockV1", "absorbanceReaderV1", "thermocyclerModuleV2Front"}
STAGING_SLOTS = ("A4", "B4", "C4", "D4")
SLOT_ORDER = [f"{row}{col}" for row in "ABCD" for col in (1, 2, 3)] + list(STAGING_SLOTS)


@dataclass
class FlexDeck:
    slots: list[str] = field(default_factory=list)
    trash_bins: list[str] = field(default_factory=list)
    """Addressable areas such as "movableTrashA3"."""
    waste_chute: bool = False


def parse_flex_deck(fixtures: list[dict[str, Any]]) -> FlexDeck:
    deck = FlexDeck()
    slots: set[str] = set()
    for fixture in fixtures:
        cutout = str(fixture.get("cutoutId", "")).removeprefix("cutout")
        kind = str(fixture.get("cutoutFixtureId", ""))
        if kind in SLOT_FIXTURES or kind in MODULE_FIXTURES:
            slots.add(cutout)
        elif kind == "trashBinAdapter":
            deck.trash_bins.append(f"movableTrash{cutout}")
        elif kind == "stagingAreaRightSlot":
            slots.update({cutout, f"{cutout[0]}4"})
        elif kind == "stagingAreaSlotWithMagneticBlockV1":
            slots.update({cutout, f"{cutout[0]}4"})
        elif kind.startswith("stagingAreaSlotWithWasteChute"):
            deck.waste_chute = True
            slots.add("D4")
        elif kind.startswith("wasteChute"):
            deck.waste_chute = True
    deck.slots = [s for s in SLOT_ORDER if s in slots]
    return deck


def waste_chute_area(channels: int) -> str:
    return {1: "1ChannelWasteChute", 8: "8ChannelWasteChute", 96: "96ChannelWasteChute"}[channels]
