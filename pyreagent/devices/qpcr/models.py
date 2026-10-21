from __future__ import annotations

from pydantic import BaseModel, Field

from ..thermocycler.models import ThermalProfile, ThermalStage, ThermalStep


class QpcrStep(ThermalStep):
    acquire: bool = Field(default=False, description="Read fluorescence at the end of this step (usually the annealing/extension step).")


class QpcrStage(ThermalStage):
    steps: list[QpcrStep] = Field(min_length=1, max_length=10, description="Steps run in order, once per cycle.")


class QpcrProfile(ThermalProfile):
    """A qPCR program: a PCR profile where some steps acquire fluorescence once per cycle."""

    stages: list[QpcrStage] = Field(min_length=1, max_length=20)

    @property
    def acquisitions(self) -> int:
        return sum(stage.repeat * sum(step.acquire for step in stage.steps) for stage in self.stages)


class QpcrResult(BaseModel):
    plate: str
    channels: list[str]
    acquisitions: int = Field(description="Fluorescence reads per well and channel (normally one per cycle).")
    fluorescence: dict[str, dict[str, list[float]]] = Field(description="Raw fluorescence: well -> channel -> one value per acquisition.")
    ct: dict[str, dict[str, float | None]] = Field(description="Estimated Ct per well and channel; null when no amplification was detected.")
    ct_method: str
    duration_s: float
