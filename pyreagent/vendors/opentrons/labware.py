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
    return fields


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
