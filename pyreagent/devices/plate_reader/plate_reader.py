"""Plate reader capability interface."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ...core.capability import Within, capability
from ...core.device import Device
from ...core.errors import NeedsAttention
from ...core.jobs import utcnow
from ...labware import DEFAULT_CATALOG, LabwareCatalog
from .backend import PlateReaderBackend
from .models import AbsorbanceRead, PlateReaderStatus


class PlateReader(Device[PlateReaderBackend]):
    """Microplate reader."""

    device_type = "plate_reader"

    def __init__(
        self,
        name: str,
        backend: PlateReaderBackend,
        *,
        labware: LabwareCatalog | None = None,
    ) -> None:
        super().__init__(name, backend)
        self.labware = labware or DEFAULT_CATALOG

    def limits(self) -> dict[str, Any]:
        low, high = self.backend.absorbance_range_nm
        return {"min_wavelength_nm": low, "max_wavelength_nm": high, "plates": [lw.id for lw in self.labware.list(kind="plate")]}

    def validate_arguments(self, capability_name: str, values: dict[str, Any]) -> None:
        if capability_name == "read_absorbance":
            labware = self.labware.get(values["plate"], kind="plate")
            if values["wells"]:
                labware.check_wells(values["wells"])

    @capability(long_running=True)
    async def read_absorbance(
        self,
        plate: Annotated[str, Field(description="Labware id of the plate in the reader, from GET /labware (list_labware).")],
        wavelength_nm: Annotated[
            int,
            Field(ge=200, le=1000, description="Measurement wavelength in nm, within limits.min_wavelength_nm .. limits.max_wavelength_nm."),
            Within("min_wavelength_nm", "max_wavelength_nm"),
        ],
        wells: Annotated[
            list[str] | None, Field(description="Wells to read, e.g. ['A1', 'B3']. Omit to read every well of the plate.")
        ] = None,
    ) -> AbsorbanceRead:
        """Measure absorbance (optical density) of a plate at one wavelength.

        The plate must already be in the reader with the tray closed. Runs as a
        job; poll it for progress and the per-well values.
        """
        labware = self.labware.get(plate)
        targets = labware.check_wells(wells) if wells else labware.wells()
        if await self.backend.tray_is_open():
            raise NeedsAttention("The tray is open.", recoverable=True,
                                 suggested_action="Make sure the plate is seated, call close_tray, then retry.")
        values = await self.backend.read_absorbance(
            plate=labware, wavelength_nm=wavelength_nm, wells=targets,
            progress=lambda f: self.report_progress(f, f"{round(f * len(targets))}/{len(targets)} wells"),
        )
        return AbsorbanceRead(plate=plate, wavelength_nm=wavelength_nm, values=values, read_at=utcnow())

    @capability
    async def open_tray(self) -> PlateReaderStatus:
        """Open the plate tray so a plate can be loaded or removed."""
        await self.backend.open_tray()
        return PlateReaderStatus(tray_open=True)

    @capability
    async def close_tray(self) -> PlateReaderStatus:
        """Close the plate tray. Required before reading."""
        await self.backend.close_tray()
        return PlateReaderStatus(tray_open=False)

    @capability(read_only=True)
    async def status(self) -> PlateReaderStatus:
        """Whether the plate tray is open."""
        return PlateReaderStatus(tray_open=await self.backend.tray_is_open())
