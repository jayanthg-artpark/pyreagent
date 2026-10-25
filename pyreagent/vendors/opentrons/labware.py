"""Opentrons labware definitions <-> pyreagent labware.

Used by ``scripts/gen_opentrons_labware.py`` (the standard catalog) and by the
Opentrons driver for custom definitions (e.g. Labware Creator JSON files),
which are added to the pyreagent catalog and uploaded to each robot run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...labware import Labware

KINDS = {"tipRack": "tip_rack", "reservoir": "reservoir", "tubeRack": "tube_rack", "wellPlate": "plate"}


def labware_fields(definition: dict[str, Any]) -> dict[str, Any]:
    """pyreagent Labware fields for an Opentrons definition (uniform grids only)."""
    load_name = definition["parameters"]["loadName"]
    ordering = definition["ordering"]
    rows, columns = len(ordering[0]), len(ordering)
    if any(len(col) != rows for col in ordering):
        raise ValueError(f"{load_name} is not a uniform grid of wells")
    first = definition["wells"][ordering[0][0]]
    kind = "tip_rack" if definition["parameters"].get("isTiprack") else KINDS.get(definition["metadata"].get("displayCategory"), "plate")
    fields: dict[str, Any] = {"id": load_name, "kind": kind, "description": definition["metadata"].get("displayName", load_name),
                              "rows": rows, "columns": columns}
    fields["tip_volume_ul" if kind == "tip_rack" else "well_volume_ul"] = float(first["totalLiquidVolume"])
    fields["geometry"] = geometry_fields(definition)
    return fields


_GEOMETRY_KEYS = ("x", "y", "z", "depth")


def geometry_fields(definition: dict[str, Any]) -> dict[str, Any] | None:
    """Physical layout from an Opentrons definition (same origin and axes as pyreagent's); None if it has none."""
    wells, ordering, dims = definition["wells"], definition["ordering"], definition.get("dimensions")
    a1 = wells[ordering[0][0]]
    if not dims or any(k not in a1 for k in _GEOMETRY_KEYS):
        return None
    geometry: dict[str, Any] = {
        "size_x_mm": dims["xDimension"], "size_y_mm": dims["yDimension"], "size_z_mm": dims["zDimension"],
        "a1_x_mm": a1["x"], "a1_y_mm": a1["y"], "well_bottom_z_mm": a1["z"], "well_depth_mm": a1["depth"],
        "well_shape": a1.get("shape", "circular"),
    }
    if len(ordering) > 1:
        geometry["pitch_x_mm"] = round(wells[ordering[1][0]]["x"] - a1["x"], 3)
    if len(ordering[0]) > 1:
        geometry["pitch_y_mm"] = round(a1["y"] - wells[ordering[0][1]]["y"], 3)
    if geometry["well_shape"] == "circular":
        geometry["well_diameter_mm"] = a1["diameter"]
    else:
        geometry["well_size_x_mm"], geometry["well_size_y_mm"] = a1["xDimension"], a1["yDimension"]
    parameters = definition["parameters"]
    if parameters.get("isTiprack"):
        geometry["tip_length_mm"] = parameters.get("tipLength")
        geometry["tip_overlap_mm"] = parameters.get("tipOverlap")
    return geometry


def labware_from_definition(definition: dict[str, Any]) -> Labware:
    return Labware.model_validate(labware_fields(definition))


def load_definition(path: str | Path) -> dict[str, Any]:
    definition = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("parameters", "ordering", "wells", "metadata", "namespace", "version"):
        if key not in definition:
            raise ValueError(f"{path} is not an Opentrons labware definition (missing {key!r})")
    return definition


def load_spec(definition: dict[str, Any]) -> dict[str, Any]:
    """The ``loadLabware`` identity of a definition."""
    return {"load_name": definition["parameters"]["loadName"], "namespace": definition["namespace"], "version": int(definition["version"])}
