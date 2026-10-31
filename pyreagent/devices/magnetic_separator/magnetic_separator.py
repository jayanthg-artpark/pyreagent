"""Magnetic separator capability interface: pull magnetic beads to the side of the wells, then release them."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ...core.capability import Within, capability
from ...core.device import Device
from .backend import MagneticSeparatorBackend
from .models import MagnetStatus


class MagneticSeparator(Device[MagneticSeparatorBackend]):
    """Raises magnets under a plate to hold magnetic beads, for bead clean-ups and extractions."""

    device_type = "magnetic_separator"

    def limits(self) -> dict[str, Any]:
        low, high = self.backend.height_range_mm
        return {"min_height_mm": low, "max_height_mm": high}

    @capability
    async def engage(
        self,
        height_mm: Annotated[
            float, Field(ge=-10, le=50, description="Magnet top height above the surface the labware rests on, mm, within limits. "
                                                    "Pick it for the plate: roughly where the beads should collect."),
            Within("min_height_mm", "max_height_mm"),
        ],
    ) -> MagnetStatus:
        """Raise the magnets to a height, so beads collect at the sides of the wells."""
        await self.backend.engage(height_mm)
        return await self.backend.status()

    @capability(interrupt=True)
    async def disengage(self) -> MagnetStatus:
        """Lower the magnets, releasing the beads. Safe at any time."""
        await self.backend.disengage()
        return await self.backend.status()

    @capability(read_only=True)
    async def status(self) -> MagnetStatus:
        """Whether the magnets are engaged, and at what height."""
        return await self.backend.status()
