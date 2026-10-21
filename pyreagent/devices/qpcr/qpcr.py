"""qPCR (real-time PCR) capability interface.

A qPCR instrument is a thermocycler that also reads fluorescence, so ``Qpcr``
extends ``Thermocycler``: it keeps run_profile, lid, status and deactivate,
and adds run_qpcr.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from pydantic import Field

from ...core.capability import capability
from ...core.errors import InvalidArgument
from ...labware import DEFAULT_CATALOG, LabwareCatalog
from ..thermocycler.thermocycler import Thermocycler
from .analysis import CT_METHOD, estimate_ct
from .backend import QpcrBackend
from .models import QpcrProfile, QpcrResult


class Qpcr(Thermocycler):
    """Real-time PCR instrument: thermocycler with fluorescence detection."""

    device_type = "qpcr"
    backend: QpcrBackend

    def __init__(
        self,
        name: str,
        backend: QpcrBackend,
        *,
        labware: LabwareCatalog | None = None,
        min_c: float = 4.0,
        max_c: float = 99.0,
        max_lid_c: float = 110.0,
        max_cycles: int = 100,
    ) -> None:
        super().__init__(name, backend, min_c=min_c, max_c=max_c, max_lid_c=max_lid_c, max_cycles=max_cycles)
        self.labware = labware or DEFAULT_CATALOG

    def limits(self) -> dict[str, Any]:
        return {**super().limits(), "channels": list(self.backend.channels)}

    def validate_arguments(self, capability_name: str, values: dict[str, Any]) -> None:
        super().validate_arguments(capability_name, values)
        if capability_name != "run_qpcr":
            return
        plate = self.labware.get(values["plate"], kind="plate")
        rows, columns = self.backend.block_format
        if (plate.rows, plate.columns) != (rows, columns):
            raise InvalidArgument(f"{plate.id} is {plate.rows}x{plate.columns}; this block takes {rows}x{columns} plates.")
        if values["wells"]:
            plate.check_wells(values["wells"])
        unknown = sorted(set(values["channels"]) - set(self.backend.channels))
        if unknown:
            raise InvalidArgument(f"Channels {unknown} are not available.", details={"channels": self.backend.channels})
        if values["profile"].acquisitions == 0:
            raise InvalidArgument("The profile never acquires fluorescence; set acquire=true on the step to read each cycle.")

    @capability(long_running=True)
    async def run_qpcr(
        self,
        plate: Annotated[str, Field(description="Labware id of the sealed plate in the block, from GET /labware.")],
        profile: Annotated[QpcrProfile, Field(description="The qPCR program; mark the step(s) that read fluorescence with acquire=true.")],
        channels: Annotated[list[str], Field(min_length=1, description="Detection channels / dyes to read, from limits.channels.")],
        wells: Annotated[list[str] | None, Field(description="Wells to report; omit for the whole plate.")] = None,
    ) -> QpcrResult:
        """Run a real-time PCR program and return amplification curves and Ct estimates. Runs as a job.

        Ct values are a simple threshold estimate (see ct_method in the result);
        use the vendor analysis software for reportable results.
        """
        await self._require_lid_closed()
        labware = self.labware.get(plate, kind="plate")
        targets = labware.check_wells(wells) if wells else labware.wells()
        loop = asyncio.get_running_loop()
        started = loop.time()
        curves = await self.backend.run_qpcr(profile=profile, plate=labware, wells=targets, channels=channels,
                                             progress=self.report_progress)
        return QpcrResult(
            plate=plate,
            channels=channels,
            acquisitions=profile.acquisitions,
            fluorescence={w: {c: [round(v, 2) for v in vs] for c, vs in by_channel.items()} for w, by_channel in curves.items()},
            ct={w: {c: estimate_ct(vs) for c, vs in by_channel.items()} for w, by_channel in curves.items()},
            ct_method=CT_METHOD,
            duration_s=round(loop.time() - started, 3),
        )
