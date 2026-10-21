from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class PipetteInfo(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9_-]+$", description="Mount or channel name, e.g. 'left'.")
    channels: Literal[1, 8] = Field(description="1 = single-channel; 8 = multi-channel (addresses a whole column).")
    min_volume_ul: float = Field(gt=0)
    max_volume_ul: float = Field(gt=0)
    default_flow_rate_ul_s: float | None = Field(default=None, gt=0)
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
