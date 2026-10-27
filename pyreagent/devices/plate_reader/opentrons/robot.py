"""Opentrons Flex Absorbance Plate Reader Module, through the robot it is plugged into.

Shares the robot's run with the pipettes, so the plate on the reader is labware the Opentrons
liquid handler device can use. Commands are ``absorbanceReader/*``; lid and plate sensors come
from ``GET /modules``. It sits in column 3 of a Flex, with its lid dock in column 4.

- Absorbance only, at the reader's fixed wavelengths (450, 562, 600 and 650 nm in the default
  hardware; ``wavelengths_nm`` for others). Every read returns all 96 wells.
- The reader must be initialised for its wavelengths with the lid closed. The driver does that
  once per run, for all wavelengths, on connect if the lid is closed (otherwise before the first
  read), so a read never re-initialises with a plate inside. Connect with the reader empty.
- The lid is moved by the Flex gripper. Without a gripper, a human moves the lid by hand and
  ``open_tray``/``close_tray`` then tell the robot where it is.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import httpx

from ....core.errors import NeedsAttention
from ....labware import Labware
from ....vendors.opentrons.client import DEFAULT_PORT
from ....vendors.opentrons.modules import ModuleLink
from ..backend import PlateReaderBackend, Progress
from ..models import AbsorbanceSpec, ReaderSpec

DEFAULT_WAVELENGTHS = (450, 562, 600, 650)


class OpentronsAbsorbanceReaderBackend(PlateReaderBackend):
    vendor = "Opentrons"
    model = "Flex Absorbance Plate Reader (via robot)"

    def __init__(
        self,
        host: str | None = None,
        *,
        slot: str,
        robot: str = "flex",
        port: int = DEFAULT_PORT,
        serial: str | None = None,
        wavelengths_nm: Sequence[int] = DEFAULT_WAVELENGTHS,
        headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Args:
            host: The Flex the reader is plugged into.
            slot: Its deck slot, A3..D3.
            wavelengths_nm: The reader's filters (up to 6), if not the default hardware's.
        """
        if not 1 <= len(wavelengths_nm) <= 6:
            raise ValueError("wavelengths_nm: the reader takes 1 to 6 wavelengths")
        self.module = ModuleLink("absorbance_reader", host=host, robot=robot, slot=slot, port=port, serial=serial,
                                 headers=headers, transport=transport)
        self.wavelengths_nm = sorted(wavelengths_nm)
        self._initialized_in_run = -1
        self.spec = ReaderSpec(absorbance=AbsorbanceSpec(min_nm=self.wavelengths_nm[0], max_nm=self.wavelengths_nm[-1],
                                                         fixed_nm=self.wavelengths_nm),
                               plate_formats=[(8, 12)])

    async def connect(self) -> None:
        await self.module.connect()
        if (await self.module.data()).get("lidStatus") == "on":
            await self._initialize()

    async def disconnect(self) -> None:
        await self.module.disconnect()

    async def _initialize(self) -> None:
        await self.module.command("absorbanceReader/initialize", measureMode="multi", sampleWavelengths=self.wavelengths_nm)
        self._initialized_in_run = self.module.generation

    # -- lid -----------------------------------------------------------------------------------------

    async def _move_lid(self, command: str, where: str) -> None:
        try:
            await self.module.command(command)
        except NeedsAttention as exc:
            if exc.details.get("error_type") != "GripperNotAttachedError":
                raise
            raise NeedsAttention(f"There is no gripper to move the reader lid {where}.", recoverable=True,
                                 suggested_action=f"Ask a human to move the lid {where} by hand, then call this again.",
                                 details=exc.details) from None

    async def open_tray(self) -> None:
        await self._move_lid("absorbanceReader/openLid", "to its dock")

    async def close_tray(self) -> None:
        await self._move_lid("absorbanceReader/closeLid", "onto the reader")

    async def tray_is_open(self) -> bool | None:
        return (await self.module.data()).get("lidStatus") != "on"

    # -- reading -------------------------------------------------------------------------------------

    async def read_absorbance(self, *, plate: Labware, wavelength_nm: int, wells: list[str], progress: Progress) -> dict[str, float]:
        if (await self.module.data()).get("platePresence") == "absent":
            raise NeedsAttention("The reader detects no plate.", recoverable=True,
                                 suggested_action="Put the plate on the reader, close the lid, then retry.")
        if self._initialized_in_run != self.module.generation:
            await self._initialize()
        data = (await self.module.command("absorbanceReader/read")).get("data") or {}
        values = data.get(str(wavelength_nm))  # {wavelength: {well: OD}}; JSON keys are strings
        if not values:
            raise NeedsAttention(f"The reader returned no data at {wavelength_nm} nm.", recoverable=True,
                                 details={"wavelengths": sorted(data)})
        progress(1.0)
        return {well: float(values[well]) for well in wells}

    def info(self) -> dict[str, Any]:
        return {**super().info(), **self.module.info()}
