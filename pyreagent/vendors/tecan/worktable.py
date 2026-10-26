"""Tecan EVO worktable: rails, carriers, Tecan labware parameters, and firmware coordinates.

Geometry and constants from PyLabRobot's Tecan resources (MIT), restated as data:

- rails are 25 mm apart; a carrier at rail r has its origin at x = (r-1)*25 - off_x + 100,
  y = off_y + 345 - size_y (mm, y toward the back); a site adds its own offset.
- LiHa firmware coordinates: x = (x_mm - 100)*10, y = (346.5 - y_mm)*10 (y grows toward the front),
  z = z_range - Z_evo + site_z*10 + tip_z*10, where Z_evo is the labware's EVOware z_start /
  z_dispense / z_max (1/10 mm, larger = deeper) and tip_z the mounted DiTi's z offset.
- RoMa firmware coordinates use the carrier's roma_x / roma_y / roma_z_safe / roma_z_end.

PyLabRobot marks the 346.5 / 347.1 y origins and the tip z offsets as not fully verified, so
they are options of the driver rather than hard-coded there.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ...core.errors import InvalidArgument
from ...devices.liquid_handler.positions import Site
from ...labware import Labware, LabwareGeometry

RAIL_PITCH_MM = 25.0
RAILS = {"evo100": 30, "evo150": 45, "evo200": 69}


@dataclass(frozen=True)
class Carrier:
    size_y: float
    off_x: float
    off_y: float
    sites: tuple[tuple[float, float, float], ...]
    """(x, y, z) of each site, front to back, relative to the carrier origin."""
    roma: tuple[float, float, float, float] | None = None
    """(roma_x, roma_y, roma_z_safe, roma_z_end) for plate carriers the RoMa can reach."""
    waste_site: int | None = None


CARRIERS: dict[str, Carrier] = {
    "MP_3Pos": Carrier(316.0, 12.0, 24.7, ((5.5, 13.5, 62.5), (5.5, 109.5, 62.5), (5.5, 205.5, 62.5)), roma=(1878, 423, 946, 2537)),
    "MP_3Pos_PCR": Carrier(316.0, 12.0, 24.7, ((5.5, 13.5, 62.5), (5.5, 109.5, 62.5), (5.5, 205.5, 62.5)), roma=(1876, 423, 946, 2566)),
    "DiTi_3Pos": Carrier(374.0, 12.0, 24.7, ((13.3, 70.0, 4.5), (13.3, 170.0, 4.5), (13.3, 270.0, 4.5))),
    "DiTi_SBS_3_Pos_MCA96": Carrier(295.0, 12.0, 11.0, ((10.5, 12.0, 6.0), (10.5, 108.0, 6.0), (10.5, 204.0, 6.0))),
}


@dataclass(frozen=True)
class TecanParams:
    """EVOware labware parameters (1/10 mm, larger = deeper) and liquid surface area per well (mm²)."""

    z_start: float
    z_dispense: float
    z_max: float
    area: float | None = None
    tip_z_mm: float | None = None
    """DiTi racks: the tip's z offset used in the firmware z formula."""


def _grid(rows: int, columns: int, size: tuple[float, float, float], dx: float, dy: float, pitch: float, depth: float) -> LabwareGeometry:
    """Tecan's dx/dy are the front-left corner of the front-left well; pyreagent stores well A1's centre."""
    return LabwareGeometry(size_x_mm=size[0], size_y_mm=size[1], size_z_mm=size[2], a1_x_mm=dx + pitch / 2,
                           a1_y_mm=dy + (rows - 1) * pitch + pitch / 2, pitch_x_mm=pitch, pitch_y_mm=pitch,
                           well_bottom_z_mm=0.0, well_depth_mm=depth, well_shape="rectangular", well_size_x_mm=pitch, well_size_y_mm=pitch)


_DITI_RACK = dict(rows=8, columns=12, size=(123.4, 89.4, 22.2), dx=7.7, dy=8.7, pitch=9.0)

TECAN_LABWARE: dict[str, tuple[Labware, TecanParams]] = {}


def _add(labware: Labware, params: TecanParams) -> None:
    TECAN_LABWARE[labware.id] = (labware, params)


for _id, _desc, _rows, _cols, _size, _dx, _dy, _pitch, _depth, _p in [
    ("tecan_microplate_96_well", "Tecan Microplate 96 Well", 8, 12, (127.8, 85.4, 7.6), 12.5, 7.6, 9.0, 22.6, TecanParams(1957, 1975, 2005, 33.2)),
    ("tecan_deepwell_96_well", "Tecan DeepWell 96 Well", 8, 12, (127.8, 85.4, 39.0), 9.9, 6.7, 9.0, 39.0, TecanParams(1670, 1690, 2060, 33.2)),
    ("tecan_pcr_plate_96_well", "Tecan PCR Plate 96 Well", 8, 12, (128.0, 83.2, 19.5), 10.1, 5.3, 9.0, 19.5, TecanParams(1900, 1915, 2095, 28.3)),
    ("tecan_plate_384_well", "Tecan Plate 384 Well", 16, 24, (127.7, 85.5, 12.1), 9.85, 6.75, 4.5, 12.1, TecanParams(1940, 1960, 2061, 13.7)),
]:
    _add(Labware(id=_id, kind="plate", description=_desc, rows=_rows, columns=_cols,
                 geometry=_grid(_rows, _cols, _size, _dx, _dy, _pitch, _depth)), _p)

for _volume, _tip_z in [(10, -31.3), (50, -5.3), (200, -5.0), (1000, 32.6)]:
    _add(Labware(id=f"tecan_diti_{_volume}ul_liha", kind="tip_rack", description=f"Tecan DiTi {_volume} µL LiHa rack",
                 rows=8, columns=12, tip_volume_ul=_volume,
                 geometry=_grid(8, 12, _DITI_RACK["size"], _DITI_RACK["dx"], _DITI_RACK["dy"], 9.0, 22.2)),  # type: ignore[arg-type]
         TecanParams(877, 865, 1087, tip_z_mm=_tip_z))
_add(Labware(id="tecan_diti_100ul_te_mo", kind="tip_rack", description="Tecan DiTi 100 µL Te-MO rack", rows=8, columns=12,
             tip_volume_ul=100, geometry=_grid(8, 12, (128.0, 85.8, 22.2), 10.0, 6.9, 9.0, 22.2)),
     TecanParams(1230, 1280, 1430, tip_z_mm=42.0))


@dataclass
class Worktable:
    """Carriers placed on rails. Slots are named "<carrier>.<site>" (sites numbered from 1, front to back)."""

    model: str = "evo150"
    carriers: dict[str, tuple[str, int]] = field(default_factory=dict)
    """name -> (carrier type, rail)."""

    def __post_init__(self) -> None:
        if self.model not in RAILS:
            raise ValueError(f"model must be one of {sorted(RAILS)}")
        for name, (kind, rail) in self.carriers.items():
            if kind not in CARRIERS:
                raise ValueError(f"carrier {name}: unknown type {kind!r}; known: {sorted(CARRIERS)}")
            if not 1 <= rail <= RAILS[self.model]:
                raise ValueError(f"carrier {name}: rail {rail} is outside 1..{RAILS[self.model]}")

    @property
    def slots(self) -> list[str]:
        return [f"{name}.{i + 1}" for name, (kind, _) in self.carriers.items() for i in range(len(CARRIERS[kind].sites))]

    def _parse(self, slot: str) -> tuple[Carrier, float, float, tuple[float, float, float]]:
        name, _, index = slot.rpartition(".")
        if name not in self.carriers or not index.isdigit():
            raise InvalidArgument(f"No worktable slot {slot!r}.", details={"slots": self.slots})
        kind, rail = self.carriers[name]
        carrier = CARRIERS[kind]
        if not 1 <= int(index) <= len(carrier.sites):
            raise InvalidArgument(f"Carrier {name} has sites 1..{len(carrier.sites)}.")
        origin_x = (rail - 1) * RAIL_PITCH_MM - carrier.off_x + 100
        origin_y = carrier.off_y + 345 - carrier.size_y
        return carrier, origin_x, origin_y, carrier.sites[int(index) - 1]

    def site(self, slot: str) -> Site:
        """Where labware in ``slot`` has its front-left-bottom corner (mm, y toward the back)."""
        _, ox, oy, (sx, sy, sz) = self._parse(slot)
        return Site(ox + sx, oy + sy, sz)

    def roma(self, slot: str) -> tuple[float, float, float, float]:
        carrier, *_ = self._parse(slot)
        if carrier.roma is None:
            raise InvalidArgument(f"The RoMa cannot reach labware on the carrier holding slot {slot}.")
        return carrier.roma
