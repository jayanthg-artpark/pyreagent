from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class NozzleLayout(BaseModel):
    """Which nozzles of a multi-channel head are used (partial tip pickup)."""

    style: Literal["all", "single", "partial_column", "column", "row"] = Field(
        default="all",
        description="all: every nozzle. single: one nozzle. partial_column: the first `nozzles` nozzles of a column "
        "(8-channel or 96-channel). column: one full column of 8 (96-channel). row: one full row of 12 (96-channel).",
    )
    nozzles: int | None = Field(default=None, ge=2, le=7, description="partial_column only: how many nozzles.")

    @model_validator(mode="after")
    def _nozzle_count(self) -> "NozzleLayout":
        if (self.style == "partial_column") != (self.nozzles is not None):
            raise ValueError("nozzles is required for partial_column and only allowed for partial_column")
        return self


class PipetteInfo(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9_-]+$", description="Mount or channel name, e.g. 'left'.")
    channels: Literal[1, 8, 96] = Field(description="1 = single-channel; 8 = addresses a column; 96 = addresses a whole plate.")
    min_volume_ul: float = Field(gt=0)
    max_volume_ul: float = Field(gt=0)
    default_flow_rate_ul_s: float | None = Field(default=None, gt=0)
    independent_channels: bool = Field(
        default=False, description="Each channel moves in Z on its own (Hamilton, Tecan), so unused channels never touch tips."
    )
    tip_volumes_ul: list[float] | None = Field(
        default=None, description="Tip sizes this pipette accepts. Default: any tip at least max_volume_ul."
    )

    def accepts_tip(self, tip_volume_ul: float) -> bool:
        if self.tip_volumes_ul is not None:
            return any(abs(tip_volume_ul - v) < 1e-6 for v in self.tip_volumes_ul)
        return tip_volume_ul >= self.max_volume_ul

    @model_validator(mode="after")
    def _range(self) -> "PipetteInfo":
        if self.min_volume_ul >= self.max_volume_ul:
            raise ValueError("min_volume_ul must be below max_volume_ul")
        return self


class PipetteState(PipetteInfo):
    layout: NozzleLayout = Field(default_factory=NozzleLayout, description="Active nozzle layout.")
    tip_attached: bool
    tip_capacity_ul: float | None = Field(description="Usable volume with the current tip (tip size capped by the pipette).")
    tip_contents_ul: float = Field(description="Liquid currently held, per channel.")


class SlotState(BaseModel):
    slot: str
    labware: str | None = None
    kind: str | None = None
    tips_remaining: int | None = Field(default=None, description="Tip racks: unused tips.")
    volumes_ul: dict[str, float] | None = Field(default=None, description="Known non-empty wells and their volume (µL).")
    unknown_volume_wells: int | None = Field(default=None, description="Wells whose volume is not tracked.")


class DeckState(BaseModel):
    slots: list[SlotState]
    pipettes: list[PipetteState]


class TransferResult(BaseModel):
    transfers: int = Field(description="Source/destination pairs completed.")
    volume_ul: float = Field(description="Volume moved per pair, per channel.")
    tips_used: int
    operations: int = Field(description="Low-level pick-up/aspirate/dispense/drop steps executed.")
    duration_s: float


class LiquidHeights(BaseModel):
    slot: str
    heights_mm: dict[str, float | None] = Field(description="Liquid surface above each well's bottom, mm; null = no liquid found.")
