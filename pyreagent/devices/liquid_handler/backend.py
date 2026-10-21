"""Contract every liquid handler driver implements.

The frontend does all bookkeeping (what is on the deck, tips, volumes) and
validation; a driver only executes motions. ``volume_ul`` is per channel, and
``well`` is the well under the pipette's primary nozzle for the active nozzle
layout (row A of a column for a full 8-channel head).

Required: home, pick_up_tips, drop_tips, aspirate, dispense, stop.
Optional operations are listed in ``features``; the frontend refuses a call for
a feature the driver does not declare before anything moves.
"""

from __future__ import annotations

import abc
from typing import Any

from ...core.backend import Backend
from ...core.errors import NotSupported
from ...labware import Labware
from .models import NozzleLayout, PipetteInfo

FEATURES = frozenset({
    "blow_out",        # blow_out
    "touch_tip",       # touch_tip
    "return_tips",     # return_tips (put tips back in their rack)
    "move_to",         # move_to (position over a well, no liquid handling)
    "move_labware",    # move_labware with use_gripper=False (record a manual move on the robot)
    "gripper",         # move_labware with use_gripper=True
    "liquid_probe",    # probe_liquid_heights
    "nozzle_layouts",  # configure_nozzles (partial tip pickup)
    "lights",          # set_lights
})


class LiquidHandlerBackend(Backend):
    slots: list[str] = []
    """Deck slot names, e.g. ["1", ..., "11"] or ["A1", ..., "D3"]. May be refined during ``connect``."""
    features: frozenset[str] = frozenset()
    """Optional operations this driver supports (subset of ``FEATURES``)."""

    @property
    @abc.abstractmethod
    def pipettes(self) -> dict[str, PipetteInfo]:
        """Installed pipettes by name. May be read from the robot during ``connect``."""

    def check_placement(self, slot: str, labware: Labware) -> None:
        """Raise ``invalid_argument`` if this driver cannot put ``labware`` in ``slot``. Must not touch the robot.

        Called when labware is declared, so unusable labware is refused before anything moves.
        """

    # -- required ------------------------------------------------------------------------------

    @abc.abstractmethod
    async def home(self) -> None: ...

    @abc.abstractmethod
    async def pick_up_tips(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str) -> None: ...

    @abc.abstractmethod
    async def drop_tips(self, *, pipette: PipetteInfo) -> None:
        """Eject tips into the trash."""

    @abc.abstractmethod
    async def aspirate(
        self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, volume_ul: float, flow_rate_ul_s: float | None
    ) -> None: ...

    @abc.abstractmethod
    async def dispense(
        self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, volume_ul: float, flow_rate_ul_s: float | None
    ) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None:
        """Halt all motion immediately."""

    # -- optional (see ``features``) -------------------------------------------------------------

    def _unsupported(self, feature: str) -> NotSupported:
        return NotSupported(f"{type(self).__name__} does not support {feature}.")

    async def blow_out(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, flow_rate_ul_s: float | None) -> None:
        raise self._unsupported("blow_out")

    async def touch_tip(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str) -> None:
        raise self._unsupported("touch_tip")

    async def return_tips(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str) -> None:
        raise self._unsupported("return_tips")

    async def move_to(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, height_mm: float) -> None:
        raise self._unsupported("move_to")

    async def move_labware(self, *, labware: Labware, from_slot: str, to_slot: str, use_gripper: bool) -> None:
        raise self._unsupported("gripper" if use_gripper else "move_labware")

    async def probe_liquid_heights(self, *, pipette: PipetteInfo, slot: str, labware: Labware, wells: list[str]) -> dict[str, float | None]:
        """Liquid surface height above each well's bottom in mm (None where no liquid was found)."""
        raise self._unsupported("liquid_probe")

    async def configure_nozzles(self, *, pipette: PipetteInfo, layout: NozzleLayout) -> None:
        raise self._unsupported("nozzle_layouts")

    async def set_lights(self, on: bool) -> None:
        raise self._unsupported("lights")

    def info(self) -> dict[str, Any]:
        return {**super().info(), "slots": list(self.slots), "features": sorted(self.features)}
