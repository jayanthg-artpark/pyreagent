"""Labware definitions, so servers can reject commands that name labware that does not exist."""

from __future__ import annotations

import re
import string
from typing import Iterable, Literal

from pydantic import BaseModel, Field, model_validator

from ..core.errors import InvalidArgument, NotFound

LabwareKind = Literal["plate", "tip_rack", "reservoir", "tube_rack"]

_WELL_RE = re.compile(r"^([A-Z]+)(\d+)$")


def row_label(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA (1536-well plates have 32 rows)."""
    letters = string.ascii_uppercase
    label = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        label = letters[rem] + label
    return label


def parse_well(name: str) -> tuple[int, int]:
    """``"B3"`` -> ``(1, 3)``: 0-based row index, 1-based column number."""
    match = _WELL_RE.match(name.strip().upper())
    if match is None:
        raise InvalidArgument(f"{name!r} is not a well name like 'A1'.")
    row = 0
    for ch in match.group(1):
        row = row * 26 + (ord(ch) - ord("A") + 1)
    return row - 1, int(match.group(2))


class Labware(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    kind: LabwareKind = "plate"
    description: str = ""
    rows: int = Field(gt=0, le=64)
    columns: int = Field(gt=0, le=96)
    well_volume_ul: float | None = Field(default=None, gt=0, description="Maximum liquid per well, if known.")
    tip_volume_ul: float | None = Field(default=None, gt=0, description="Tip racks only: capacity of each tip.")

    @model_validator(mode="after")
    def _tip_volume_matches_kind(self) -> "Labware":
        if (self.kind == "tip_rack") != (self.tip_volume_ul is not None):
            raise ValueError("tip_volume_ul is required for tip racks and only allowed for tip racks")
        return self

    @property
    def well_count(self) -> int:
        return self.rows * self.columns

    def wells(self) -> list[str]:
        """Row-major well names: A1, A2, ... A12, B1, ..."""
        return [f"{row_label(r)}{c + 1}" for r in range(self.rows) for c in range(self.columns)]

    def wells_by_column(self) -> list[str]:
        """Column-major well names: A1, B1, ... H1, A2, ... (the order tips are used in)."""
        return [f"{row_label(r)}{c + 1}" for c in range(self.columns) for r in range(self.rows)]

    def check_wells(self, wells: Iterable[str]) -> list[str]:
        """Normalise well names and raise ``invalid_argument`` for any that this labware lacks."""
        valid = set(self.wells())
        normalised = [w.strip().upper() for w in wells]
        bad = [w for w in normalised if w not in valid]
        if bad:
            raise InvalidArgument(
                f"{self.id} has no well(s) {', '.join(bad)}; it has {self.rows} rows x {self.columns} columns.",
                details={"invalid_wells": bad},
            )
        return normalised

    def check_columns(self, columns: Iterable[int]) -> list[int]:
        cols = list(columns)
        bad = [c for c in cols if not 1 <= c <= self.columns]
        if bad:
            raise InvalidArgument(f"{self.id} has columns 1..{self.columns}; got {bad}.", details={"invalid_columns": bad})
        return cols


class LabwareCatalog:
    def __init__(self, items: Iterable[Labware] = ()) -> None:
        self._items: dict[str, Labware] = {}
        for item in items:
            self.add(item)

    def add(self, labware: Labware) -> None:
        self._items[labware.id] = labware

    def get(self, labware_id: str, *, kind: LabwareKind | Iterable[LabwareKind] | None = None) -> Labware:
        try:
            labware = self._items[labware_id]
        except KeyError:
            raise NotFound(
                f"Unknown labware {labware_id!r}.",
                suggested_action="Use an id from GET /labware (list_labware tool), or ask a human to register the labware.",
                details={"known": sorted(self._items)},
            ) from None
        kinds = {kind} if isinstance(kind, str) else set(kind or ())
        if kinds and labware.kind not in kinds:
            raise InvalidArgument(f"{labware_id} is a {labware.kind}; this needs a {' or '.join(sorted(kinds))}.")
        return labware

    def list(self, *, kind: LabwareKind | None = None) -> list[Labware]:
        return sorted((lw for lw in self._items.values() if kind is None or lw.kind == kind), key=lambda lw: lw.id)


DEFAULT_CATALOG = LabwareCatalog(
    [
        Labware(id="corning_96_wellplate_360ul", description="Corning 96-well flat bottom plate", rows=8, columns=12, well_volume_ul=360),
        Labware(id="generic_6_wellplate", description="Generic 6-well plate", rows=2, columns=3),
        Labware(id="generic_24_wellplate", description="Generic 24-well plate", rows=4, columns=6),
        Labware(id="generic_96_wellplate", description="Generic 96-well plate", rows=8, columns=12),
        Labware(id="generic_384_wellplate", description="Generic 384-well plate", rows=16, columns=24),
        Labware(id="generic_96_pcr_plate_200ul", description="Generic 96-well PCR plate, 0.2 mL wells", rows=8, columns=12, well_volume_ul=200),
        Labware(id="generic_96_deepwell_2ml", description="Generic 96 deep-well plate, 2 mL wells", rows=8, columns=12, well_volume_ul=2000),
        Labware(id="generic_12_reservoir_15ml", kind="reservoir", description="Generic 12-trough reservoir", rows=1, columns=12, well_volume_ul=15000),
        Labware(id="generic_1_reservoir_200ml", kind="reservoir", description="Generic single-trough reservoir", rows=1, columns=1, well_volume_ul=200000),
        Labware(id="generic_96_tiprack_20ul", kind="tip_rack", description="Generic 96 tip rack, 20 µL tips", rows=8, columns=12, tip_volume_ul=20),
        Labware(id="generic_96_tiprack_300ul", kind="tip_rack", description="Generic 96 tip rack, 300 µL tips", rows=8, columns=12, tip_volume_ul=300),
        Labware(id="generic_96_tiprack_1000ul", kind="tip_rack", description="Generic 96 tip rack, 1000 µL tips", rows=8, columns=12, tip_volume_ul=1000),
    ]
)

__all__ = ["DEFAULT_CATALOG", "Labware", "LabwareCatalog", "LabwareKind", "parse_well", "row_label"]
