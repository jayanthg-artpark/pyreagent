"""Absolute positions for coordinate-driven liquid handlers (Hamilton, Tecan).

Robots that are told *where* to move, rather than *which labware* to use,
need every slot's position on their deck. A ``Site`` is where a slot puts the
front-left-bottom corner of its labware, in the robot's own frame (mm). Vendor
packages build sites from their deck model (Hamilton rails and carriers, Tecan
grid positions); this module turns site + labware + well into coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...core.errors import InvalidArgument
from ...labware import Labware


@dataclass(frozen=True)
class Site:
    x_mm: float
    y_mm: float
    z_mm: float
    """Height of the surface the labware stands on."""

    def well(self, labware: Labware, well: str) -> tuple[float, float, float]:
        """(x, y, z of the well bottom) of ``well`` in robot coordinates."""
        x, y, z = labware.well_position(well)
        return (round(self.x_mm + x, 3), round(self.y_mm + y, 3), round(self.z_mm + z, 3))

    def top(self, labware: Labware) -> float:
        """Height of the labware's top surface."""
        return round(self.z_mm + labware.require_geometry().size_z_mm, 3)


@dataclass(frozen=True)
class ChannelTarget:
    channel: int
    """0-based channel index on the head (0 = the channel nearest the back)."""
    well: str
    x_mm: float
    y_mm: float
    bottom_z_mm: float


def channel_targets(site: Site, labware: Labware, wells: list[str], *, pitch_mm: float = 9.0) -> list[ChannelTarget]:
    """Per-channel positions for wells touched together, back to front (channel 0 is the back-most).

    ``wells`` is what ``nozzles.wells_touched`` returned for the active nozzles. When several
    channels share one well (a trough), they are spread across it ``pitch_mm`` apart, centred,
    and the well must be long enough to hold them.
    """
    counts: dict[str, int] = {}
    for w in wells:
        counts[w] = counts.get(w, 0) + 1
    geometry = labware.require_geometry()
    targets: list[tuple[str, float, float, float]] = []
    for w, n in counts.items():
        x, y, z = site.well(labware, w)
        if n > 1:
            room = geometry.well_size_y_mm or geometry.well_diameter_mm or 0.0
            if (n - 1) * pitch_mm > room - 2.0:
                raise InvalidArgument(f"{n} channels do not fit side by side in {labware.id} well {w} ({room:g} mm long).")
        for i in range(n):
            targets.append((w, x, round(y + ((n - 1) / 2 - i) * pitch_mm, 3), z))
    targets.sort(key=lambda t: -t[2])  # back (largest y) first
    return [ChannelTarget(i, w, x, y, z) for i, (w, x, y, z) in enumerate(targets)]


def require_spacing(targets: list[ChannelTarget], minimum_mm: float) -> None:
    """Independent channels cannot be closer than ``minimum_mm`` in y."""
    ys = sorted((t.y_mm for t in targets), reverse=True)
    for back, front in zip(ys, ys[1:]):
        if back - front < minimum_mm - 1e-6:
            raise InvalidArgument(f"Channels would be {back - front:.2f} mm apart; this head needs at least {minimum_mm} mm.")
