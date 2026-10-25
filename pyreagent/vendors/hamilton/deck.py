"""Hamilton STAR / STARlet deck: tracks, carriers, Hamilton tip racks, and where labware ends up.

Geometry from PyLabRobot's Hamilton resources (MIT), restated as data (mm; x right, y back, z up;
firmware coordinates are these × 10):

- carrier on track t: origin x = 100 + (t-1) × 22.5, y = 63, z = 100. STARlet 30 tracks, STAR 54.
- tip carriers: the rack is centred over the site opening and sinks into it (6 mm for Hamilton racks).
- plate carriers: the plate's front-left corner sits at the site; it sinks onto the pedestal until
  the pedestal touches the well bottoms (at most the pedestal height).
- channel trash (8 channels spread 9 mm apart around its centre), 96-head trash at A1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ...core.errors import InvalidArgument
from ...devices.liquid_handler.positions import Site
from ...labware import Labware, LabwareGeometry

TRACK_PITCH_MM = 22.5
TRACKS = {"starlet": 30, "star": 54}
TRASH = {"starlet": (800.0, 311.2, 137.1), "star": (1340.0, 311.2, 137.1)}
TRASH96_A1 = (-42.0, 120.3, 216.4)
"""Where the 96 head's A1 nozzle drops tips into the CoRe 96 waste (x is left of the deck origin)."""
CARRIER_Y_MM = 63.0
CARRIER_Z_MM = 100.0


@dataclass(frozen=True)
class Carrier:
    kind: Literal["tip", "plate"]
    sites: tuple[tuple[float, float, float], ...]
    """(x, y, z) of each site's front-left corner relative to the carrier origin, front to back."""
    site_size: tuple[float, float]
    tracks: int = 6
    pedestal_mm: float = 0.0


CARRIERS: dict[str, Carrier] = {
    "TIP_CAR_480_A00": Carrier("tip", tuple((10.15, y, 114.95) for y in (14.3, 110.3, 206.3, 302.3, 398.3)), (114.5, 74.0)),
    "PLT_CAR_L5AC_A00": Carrier("plate", tuple((4.0, y, 86.15) for y in (8.5, 104.5, 200.5, 296.5, 392.5)), (127.0, 86.0),
                                pedestal_mm=4.74),
    "PLT_CAR_L5MD_A00": Carrier("plate", tuple((4.0, y, 111.75) for y in (8.5, 104.5, 200.5, 296.5, 392.5)), (127.0, 86.0),
                                pedestal_mm=6.55),
}


@dataclass(frozen=True)
class HamiltonTip:
    """What the firmware's tip-type table (C0TT) needs, plus the rack-specific heights."""

    volume_ul: float
    """Nominal volume, used to choose a liquid class."""
    max_volume_ul: float
    length_mm: float
    has_filter: bool
    size: int
    """Firmware tip size: 1 low volume, 2 standard, 3 high volume."""
    collar_mm: float
    fitting_depth_mm: float = 8.0
    rack_sink_mm: float = 6.0
    """How far the rack sinks into a tip carrier site."""


HAMILTON_TIPS: dict[str, HamiltonTip] = {}
HAMILTON_LABWARE: list[Labware] = []

_RACK = LabwareGeometry(size_x_mm=122.4, size_y_mm=82.6, size_z_mm=20.0, a1_x_mm=11.7, a1_y_mm=72.8, well_bottom_z_mm=7.5,
                        well_depth_mm=7.5, well_diameter_mm=7.2)

for _volume, _max, _max_filter, _length, _size, _collar in [(10, 15, 10, 29.9, 1, 6.0), (50, 65, 60, 50.4, 2, 8.0),
                                                          (300, 400, 360, 59.9, 2, 8.0), (1000, 1250, 1065, 95.1, 3, 10.0)]:
    for _filter in (False, True):
        _id = f"hamilton_96_tiprack_{_volume}ul" + ("_filter" if _filter else "")
        HAMILTON_TIPS[_id] = HamiltonTip(_volume, _max_filter if _filter else _max, _length, _filter, _size, _collar)
        HAMILTON_LABWARE.append(Labware(
            id=_id, kind="tip_rack", rows=8, columns=12, tip_volume_ul=_volume,
            description=f"Hamilton {_volume} µL {'filter ' if _filter else ''}tips, 96 rack (CO-RE)",
            geometry=_RACK.model_copy(update={"tip_length_mm": _length, "tip_overlap_mm": 8.0}),
        ))


@dataclass
class StarDeck:
    """Carriers placed on tracks. Slots are named "<carrier>.<site>" (sites numbered from 1, front to back)."""

    model: str = "starlet"
    carriers: dict[str, tuple[str, int]] = field(default_factory=dict)
    """name -> (carrier type, first track)."""

    def __post_init__(self) -> None:
        if self.model not in TRACKS:
            raise ValueError(f"model must be one of {sorted(TRACKS)}")
        for name, (kind, track) in self.carriers.items():
            if kind not in CARRIERS:
                raise ValueError(f"carrier {name}: unknown type {kind!r}; known: {sorted(CARRIERS)}")
            if not 1 <= track <= TRACKS[self.model] - CARRIERS[kind].tracks + 1:
                raise ValueError(f"carrier {name}: track {track} does not fit on a {self.model} ({TRACKS[self.model]} tracks)")

    @property
    def slots(self) -> list[str]:
        return [f"{name}.{i + 1}" for name, (kind, _) in self.carriers.items() for i in range(len(CARRIERS[kind].sites))]

    @property
    def trash(self) -> tuple[float, float, float]:
        return TRASH[self.model]

    def carrier(self, slot: str) -> tuple[Carrier, tuple[float, float, float]]:
        """The carrier holding ``slot`` and the slot's absolute site corner."""
        name, _, index = slot.rpartition(".")
        if name not in self.carriers or not index.isdigit():
            raise InvalidArgument(f"No deck slot {slot!r}.", details={"slots": self.slots})
        kind, track = self.carriers[name]
        carrier = CARRIERS[kind]
        if not 1 <= int(index) <= len(carrier.sites):
            raise InvalidArgument(f"Carrier {name} has sites 1..{len(carrier.sites)}.")
        sx, sy, sz = carrier.sites[int(index) - 1]
        return carrier, (100 + (track - 1) * TRACK_PITCH_MM + sx, CARRIER_Y_MM + sy, CARRIER_Z_MM + sz)

    def site(self, slot: str, labware: Labware) -> Site:
        """Where ``labware`` in ``slot`` has its front-left-bottom corner."""
        carrier, (x, y, z) = self.carrier(slot)
        geometry = labware.require_geometry()
        if carrier.kind == "tip":
            tip = HAMILTON_TIPS.get(labware.id)
            sink = tip.rack_sink_mm if tip else 0.0
            w, d = carrier.site_size
            return Site(x + (w - geometry.size_x_mm) / 2, y + (d - geometry.size_y_mm) / 2, z - sink)
        sink = min(carrier.pedestal_mm, max(geometry.well_bottom_z_mm - geometry.well_floor_mm, 0.0))
        return Site(x, y, z - sink)

    def check(self, slot: str, labware: Labware) -> None:
        carrier, _ = self.carrier(slot)
        if (carrier.kind == "tip") != (labware.kind == "tip_rack"):
            raise InvalidArgument(f"Slot {slot} is on a {carrier.kind} carrier; {labware.id} is a {labware.kind}.")
        if labware.kind == "tip_rack" and labware.id not in HAMILTON_TIPS:
            raise InvalidArgument(f"{labware.id} is not a Hamilton tip rack the STAR knows.",
                                  suggested_action=f"Use one of {sorted(HAMILTON_TIPS)}.")
        labware.require_geometry()
