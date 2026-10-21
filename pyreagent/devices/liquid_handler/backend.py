"""Contract every liquid handler driver implements.

The frontend does all bookkeeping (what is on the deck, tips, volumes) and
validation; a driver only executes motions. ``volume_ul`` is per channel, and
``well`` is the well under the first channel (row A for an 8-channel head).
"""

from __future__ import annotations

import abc
from typing import Any

from ...core.backend import Backend
from ...labware import Labware
from .models import PipetteInfo


class LiquidHandlerBackend(Backend):
    slots: list[str] = []
    """Deck slot names, e.g. ["1", ..., "11"] or ["A1", ..., "D3"]."""

    @property
    @abc.abstractmethod
    def pipettes(self) -> dict[str, PipetteInfo]:
        """Installed pipettes by name. May be read from the robot during ``connect``."""

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

    def info(self) -> dict[str, Any]:
        return {**super().info(), "slots": list(self.slots)}
