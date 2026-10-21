"""Plate washer capability interface."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from pydantic import Field

from ...core.capability import Within, capability
from ...core.device import Device
from ...core.errors import InvalidArgument, NeedsAttention
from ...labware import DEFAULT_CATALOG, Labware, LabwareCatalog
from .backend import PlateWasherBackend
from .models import PlateWasherStatus, WasherRun

Plate = Annotated[str, Field(description="Labware id of the plate on the carrier, from GET /labware.")]
Buffer = Annotated[str, Field(description="Buffer bottle, from limits.buffers.")]
Columns = Annotated[list[int] | None, Field(description="1-based plate columns to process (strip washing); omit for the whole plate.")]
DispenseUl = Annotated[
    float, Field(gt=0, description="Buffer per well, µL, up to limits.max_dispense_ul and the well's capacity."), Within(high="max_dispense_ul")
]


class PlateWasher(Device[PlateWasherBackend]):
    """Microplate washer: aspirates wells and refills them with buffer through a manifold."""

    device_type = "plate_washer"

    def __init__(
        self, name: str, backend: PlateWasherBackend, *, labware: LabwareCatalog | None = None
    ) -> None:
        super().__init__(name, backend)
        self.labware = labware or DEFAULT_CATALOG

    def limits(self) -> dict[str, Any]:
        return {"buffers": list(self.backend.buffers), "max_dispense_ul": self.backend.max_dispense_ul,
                "plate_formats": list(self.backend.plate_formats)}

    def _plate(self, plate_id: str) -> Labware:
        plate = self.labware.get(plate_id, kind="plate")
        if plate.well_count not in self.backend.plate_formats:
            raise InvalidArgument(f"{plate.id} has {plate.well_count} wells; this washer handles {list(self.backend.plate_formats)}-well plates.")
        return plate

    def validate_arguments(self, capability_name: str, values: dict[str, Any]) -> None:
        if "buffer" in values and values["buffer"] not in self.backend.buffers:
            raise InvalidArgument(f"No buffer {values['buffer']!r}.", details={"buffers": self.backend.buffers})
        if "plate" in values:
            plate = self._plate(values["plate"])
            if values.get("columns"):
                plate.check_columns(values["columns"])
            volume = values.get("dispense_volume_ul") or values.get("volume_ul")
            if volume and plate.well_volume_ul and volume > plate.well_volume_ul:
                raise InvalidArgument(f"{volume} µL would overflow {plate.id} wells ({plate.well_volume_ul} µL).")

    async def _check_supplies(self, buffer: str, needed_ml: float) -> None:
        status = await self.backend.status()
        if status.waste_full or (
            status.waste_ml is not None and status.waste_capacity_ml is not None and status.waste_ml + needed_ml > status.waste_capacity_ml
        ):
            raise NeedsAttention("The waste bottle is full or would overflow during this run.",
                                 suggested_action="Ask a human to empty the waste bottle, then retry.")
        have = status.buffers_ml.get(buffer)
        if have is not None and have < needed_ml:
            raise NeedsAttention(f"Buffer {buffer} has {have:g} mL; this run needs about {needed_ml:g} mL.",
                                 suggested_action=f"Ask a human to refill buffer bottle {buffer}, then retry.")

    @capability(long_running=True)
    async def wash(
        self,
        plate: Plate,
        buffer: Buffer,
        dispense_volume_ul: DispenseUl,
        cycles: Annotated[int, Field(ge=1, le=10, description="Aspirate/dispense cycles.")] = 3,
        columns: Columns = None,
        soak_s: Annotated[float, Field(ge=0, le=600, description="Soak time after each dispense, seconds.")] = 0,
        final_aspirate: Annotated[bool, Field(description="Leave wells empty at the end (true) or full of buffer (false).")] = True,
    ) -> WasherRun:
        """Wash a plate: repeated aspirate + buffer dispense cycles. Runs as a job."""
        labware = self._plate(plate)
        cols = columns or list(range(1, labware.columns + 1))
        needed_ml = labware.rows * len(cols) * cycles * dispense_volume_ul / 1000
        await self._check_supplies(buffer, needed_ml)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await self.backend.wash(plate=labware, columns=cols, buffer=buffer, cycles=cycles, dispense_volume_ul=dispense_volume_ul,
                                soak_s=soak_s, final_aspirate=final_aspirate, progress=self.report_progress)
        return WasherRun(operation="wash", plate=plate, buffer=buffer, columns=cols, cycles=cycles,
                         buffer_used_ml=round(needed_ml, 3), duration_s=round(loop.time() - started, 3))

    @capability(long_running=True)
    async def dispense(self, plate: Plate, buffer: Buffer, volume_ul: DispenseUl, columns: Columns = None) -> WasherRun:
        """Dispense buffer into wells without aspirating first (e.g. to add blocking buffer). Runs as a job."""
        labware = self._plate(plate)
        cols = columns or list(range(1, labware.columns + 1))
        needed_ml = labware.rows * len(cols) * volume_ul / 1000
        await self._check_supplies(buffer, 0)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await self.backend.dispense(plate=labware, columns=cols, buffer=buffer, volume_ul=volume_ul, progress=self.report_progress)
        return WasherRun(operation="dispense", plate=plate, buffer=buffer, columns=cols, cycles=1,
                         buffer_used_ml=round(needed_ml, 3), duration_s=round(loop.time() - started, 3))

    @capability(long_running=True)
    async def prime(
        self, buffer: Buffer, volume_ml: Annotated[float, Field(gt=0, le=500, description="Buffer to push through, mL.")] = 50
    ) -> WasherRun:
        """Fill the washer lines with a buffer. Do this when changing buffers or after the washer sat idle."""
        await self._check_supplies(buffer, volume_ml)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await self.backend.prime(buffer=buffer, volume_ml=volume_ml)
        return WasherRun(operation="prime", plate=None, buffer=buffer, columns=[], cycles=1, buffer_used_ml=volume_ml,
                         duration_s=round(loop.time() - started, 3))

    @capability(read_only=True)
    async def status(self) -> PlateWasherStatus:
        """Buffer remaining per bottle and how full the waste bottle is."""
        return await self.backend.status()

    @capability(interrupt=True)
    async def stop(self) -> PlateWasherStatus:
        """Stop now. Safe at any time; a running wash/dispense/prime job is cancelled and wells may be part-filled."""
        self.cancel_activity()
        await self.backend.stop()
        return await self.backend.status()

    async def on_cancel(self, capability_name: str) -> None:
        if capability_name in ("wash", "dispense", "prime"):
            await self.backend.stop()
