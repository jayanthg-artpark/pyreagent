"""Nozzle geometry: which wells a head touches for a given nozzle layout.

A head is a grid of nozzles (1x1, 8x1, or 8x12). A layout picks a *primary*
nozzle, which is the one positioned over the well named in a command, and the
set of *active* nozzles that carry tips. The remaining nozzles still hang over
neighbouring positions. For tip pickup those "shadow" positions must already be
empty, or the inactive nozzles would press into tips there too.

Primary nozzles follow the Opentrons conventions (and their valid nozzle maps):

    8-channel  all             primary A1, nozzles A1-H1
               single          primary H1, nozzle H1           (other nozzles behind, toward row A)
               partial_column  primary H1, nozzles H1 + N-1 behind it (H1toG1 ... H1toB1)
    96-channel all             primary A1, every nozzle
               column          primary A12, nozzle column 12   (other columns to the left)
               row             primary H1, nozzle row H        (other rows behind, toward row A)
               single          primary H12, nozzle H12         (other nozzles behind and to the left)

So with the default order (A1, B1, ..., H1, A2, ...) the shadow of each pickup
has already been used, exactly as the robot expects.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...core.errors import InvalidArgument, NotSupported
from ...labware import Labware, parse_well, row_label
from .models import NozzleLayout


@dataclass(frozen=True)
class NozzleMap:
    primary: tuple[int, int]
    """(row, column) of the primary nozzle in the head grid."""
    active: tuple[tuple[int, int], ...]
    """Offsets (rows, columns) of active nozzles relative to the primary."""
    shadow: tuple[tuple[int, int], ...]
    """Offsets of the inactive nozzles."""

    @property
    def primary_name(self) -> str:
        return f"{row_label(self.primary[0])}{self.primary[1] + 1}"


def nozzle_map(channels: int, layout: NozzleLayout) -> NozzleMap:
    rows, cols = {1: (1, 1), 8: (8, 1), 96: (8, 12)}[channels]
    grid = [(r, c) for r in range(rows) for c in range(cols)]
    style = layout.style
    if channels == 1:
        if style not in ("all", "single"):
            raise InvalidArgument("A single-channel pipette only has one nozzle.")
        primary, active = (0, 0), [(0, 0)]
    elif channels == 8:
        if style == "all":
            primary, active = (0, 0), grid
        elif style == "single":
            primary, active = (7, 0), [(7, 0)]
        elif style == "partial_column":
            n = layout.nozzles or 0
            primary, active = (7, 0), [(7 - i, 0) for i in range(n)]
        else:
            raise InvalidArgument(f"An 8-channel pipette has no {style} layout (it has one column of 8 nozzles).")
    else:
        if style == "all":
            primary, active = (0, 0), grid
        elif style == "column":
            primary, active = (0, 11), [(r, 11) for r in range(8)]
        elif style == "row":
            primary, active = (7, 0), [(7, c) for c in range(12)]
        elif style == "single":
            primary, active = (7, 11), [(7, 11)]
        else:
            raise NotSupported("The 96-channel head has no partial_column layout; use column, row or single.")
    pr, pc = primary
    offsets = lambda cells: tuple((r - pr, c - pc) for r, c in cells)  # noqa: E731
    return NozzleMap(primary=primary, active=offsets(active), shadow=offsets([g for g in grid if g not in active]))


def _steps(labware: Labware, channels: int) -> tuple[int, int]:
    """Well pitch per nozzle pitch: 1 on 96-well grids, 2 on 384, 0 where all nozzles share one trough row/column."""
    head_rows, head_cols = {1: (1, 1), 8: (8, 1), 96: (8, 12)}[channels]
    row_step = 0 if labware.rows == 1 else (labware.rows // 8 if labware.rows >= 8 and labware.rows % 8 == 0 else 1)
    col_step = 0 if labware.columns == 1 else (labware.columns // 12 if labware.columns >= 12 and labware.columns % 12 == 0 else 1)
    return (row_step if head_rows > 1 else 1), (col_step if head_cols > 1 else 1)


def _resolve(labware: Labware, well: str, channels: int, offsets: tuple[tuple[int, int], ...]) -> list[str | None]:
    (well,) = labware.check_wells([well])
    row, column = parse_well(well)
    row_step, col_step = _steps(labware, channels)
    out: list[str | None] = []
    for dr, dc in offsets:
        r, c = row + dr * row_step, (column - 1) + dc * col_step
        out.append(f"{row_label(r)}{c + 1}" if 0 <= r < labware.rows and 0 <= c < labware.columns else None)
    return out


def wells_touched(labware: Labware, well: str, channels: int, layout: NozzleLayout) -> list[str]:
    """Wells under the active nozzles when the primary nozzle is over ``well`` (repeats for shared troughs)."""
    nmap = nozzle_map(channels, layout)
    if channels > 1 and layout.style == "all" and labware.rows != 1:
        if labware.rows % 8:
            raise InvalidArgument(f"A full {channels}-channel head cannot address {labware.id} ({labware.rows} rows).")
        _require_grid_start(labware, well, channels)
    resolved = _resolve(labware, well, channels, nmap.active)
    if None in resolved:
        raise InvalidArgument(
            f"With the {layout.style} layout, nozzle {nmap.primary_name} over {well} puts other active nozzles "
            f"outside {labware.id}; pick a well where all {len(nmap.active)} active nozzles fit.",
        )
    return [w for w in resolved if w is not None]


def _require_grid_start(labware: Labware, well: str, channels: int) -> None:
    row_step, col_step = _steps(labware, channels)
    (well,) = labware.check_wells([well])
    row, column = parse_well(well)
    if row >= max(row_step, 1) or (channels == 96 and (column - 1) >= max(col_step, 1)):
        top = "A1" if channels == 96 else "row A"
        raise InvalidArgument(f"Position a full {channels}-channel head with its first nozzle at {top}"
                              + (" (or the first 2x2 block on 384-well labware)" if row_step == 2 else "") + f", not {well}.")


def tip_shadow(labware: Labware, well: str, channels: int, layout: NozzleLayout) -> list[str]:
    """Rack positions under inactive nozzles at a pickup; they must hold no tips."""
    nmap = nozzle_map(channels, layout)
    return [w for w in _resolve(labware, well, channels, nmap.shadow) if w is not None]
