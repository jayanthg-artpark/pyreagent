"""Deck bookkeeping: what is in each slot, liquid per well, used tips, what each tip holds.

Every operation validates first and only then mutates, so running an operation
on a ``copy()`` is a dry run. The frontend uses that to refuse impossible plans
(no tips left, source too empty, destination would overflow) before a human is
asked to approve them, and again right before each motion.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ...core.errors import InvalidArgument, NeedsAttention
from ...labware import Labware, LabwareCatalog, parse_well, row_label
from .models import DeckState, PipetteInfo, PipetteState, SlotState

EPS = 1e-6
LIQUID_KINDS = ("plate", "reservoir", "tube_rack")


@dataclass
class Placed:
    labware: Labware
    volumes: dict[str, float | None]
    used_tips: set[str] = field(default_factory=set)


@dataclass
class Tip:
    attached: bool = False
    capacity_ul: float | None = None
    contents_ul: float = 0.0


def channel_wells(labware: Labware, well: str, channels: int) -> list[str]:
    """Wells touched when the first channel is at ``well`` (repeats for a trough shared by all channels)."""
    (well,) = labware.check_wells([well])
    if channels == 1:
        return [well]
    if labware.rows == 1:
        return [well] * channels
    if labware.rows % channels:
        raise InvalidArgument(f"A {channels}-channel pipette cannot address {labware.id} ({labware.rows} rows).")
    step = labware.rows // channels
    row, column = parse_well(well)
    if row >= step:
        top = "row A" if step == 1 else f"rows A-{row_label(step - 1)}"
        raise InvalidArgument(f"Position a {channels}-channel pipette with its first channel in {top}, not {well}.")
    return [f"{row_label(row + i * step)}{column}" for i in range(channels)]


class DeckModel:
    def __init__(self, slots: list[str], catalog: LabwareCatalog) -> None:
        self.slots = list(slots)
        self.catalog = catalog
        self.placed: dict[str, Placed] = {}
        self.tips: dict[str, Tip] = {}

    def copy(self) -> "DeckModel":
        clone = DeckModel(self.slots, self.catalog)
        clone.placed = {s: Placed(p.labware, dict(p.volumes), set(p.used_tips)) for s, p in self.placed.items()}
        clone.tips = {n: Tip(t.attached, t.capacity_ul, t.contents_ul) for n, t in self.tips.items()}
        return clone

    # -- labware --------------------------------------------------------------------

    def _check_slot(self, slot: str) -> None:
        if slot not in self.slots:
            raise InvalidArgument(f"No deck slot {slot!r}.", details={"slots": self.slots})

    def get(self, slot: str, *, kinds: tuple[str, ...] | None = None) -> Placed:
        self._check_slot(slot)
        placed = self.placed.get(slot)
        if placed is None:
            raise InvalidArgument(
                f"Slot {slot} is empty.",
                suggested_action="Pick another slot, or ask a human to place labware there and call load_labware.",
            )
        if kinds and placed.labware.kind not in kinds:
            raise InvalidArgument(f"Slot {slot} holds {placed.labware.id} ({placed.labware.kind}); this needs a {' or '.join(kinds)}.")
        return placed

    def place(self, slot: str, labware_id: str, *, initial_volume_ul: float | None, volumes_ul: dict[str, float] | None) -> None:
        self._check_slot(slot)
        labware = self.catalog.get(labware_id)
        volumes: dict[str, float | None] = {}
        if labware.kind == "tip_rack":
            if initial_volume_ul is not None or volumes_ul:
                raise InvalidArgument("Tip racks hold tips, not liquid; omit initial_volume_ul and volumes_ul.")
        else:
            volumes = {w: initial_volume_ul for w in labware.wells()}
            for name, volume in (volumes_ul or {}).items():
                (well,) = labware.check_wells([name])
                volumes[well] = volume
            capacity = labware.well_volume_ul
            over = {w: v for w, v in volumes.items() if v is not None and capacity is not None and v > capacity + EPS}
            if over:
                raise InvalidArgument(f"{labware.id} wells hold at most {capacity} µL; got {over}.")
        self.placed[slot] = Placed(labware, volumes)

    def remove(self, slot: str) -> None:
        self._check_slot(slot)
        self.placed.pop(slot, None)

    # -- tips -------------------------------------------------------------------------

    def tip(self, pipette: str) -> Tip:
        return self.tips.setdefault(pipette, Tip())

    def next_tip(self, pipette: PipetteInfo, slot: str | None = None) -> tuple[str, str]:
        """First unused tip (or full column, for 8 channels), column by column.

        Without ``slot``: racks of compatible tips, smallest tip size first, then in slot order.
        """
        if slot is not None:
            candidates = [slot]
            self._check_tips_fit(pipette, self.get(slot, kinds=("tip_rack",)).labware)
        else:
            racks = [(p.labware.tip_volume_ul or 0.0, self.slots.index(s), s) for s, p in self.placed.items()
                     if p.labware.kind == "tip_rack" and pipette.accepts_tip(p.labware.tip_volume_ul or 0.0)]
            candidates = [s for _, _, s in sorted(racks)]
        for s in candidates:
            placed = self.placed[s]
            for well in placed.labware.wells_by_column():
                if pipette.channels > 1 and parse_well(well)[0] != 0:
                    continue
                if not placed.used_tips.intersection(channel_wells(placed.labware, well, pipette.channels)):
                    return s, well
        raise NeedsAttention(
            f"No {'full column of ' if pipette.channels > 1 else ''}unused tips left for pipette {pipette.name}"
            + (f" in slot {slot}." if slot else "."),
            suggested_action="Ask a human to replace the tip rack, then call load_labware for that slot to reset tip tracking.",
        )

    def pick_up(self, pipette: PipetteInfo, slot: str, well: str) -> None:
        tip = self.tip(pipette.name)
        if tip.attached:
            raise InvalidArgument(f"Pipette {pipette.name} already has a tip; call drop_tip first.")
        placed = self.get(slot, kinds=("tip_rack",))
        wells = channel_wells(placed.labware, well, pipette.channels)
        used = sorted(placed.used_tips.intersection(wells))
        if used:
            raise InvalidArgument(f"Tip(s) {', '.join(used)} in slot {slot} were already used.")
        self._check_tips_fit(pipette, placed.labware)
        tip_volume = placed.labware.tip_volume_ul or 0.0
        placed.used_tips.update(wells)
        tip.attached, tip.capacity_ul, tip.contents_ul = True, min(tip_volume, pipette.max_volume_ul), 0.0

    @staticmethod
    def _check_tips_fit(pipette: PipetteInfo, labware: Labware) -> None:
        if not pipette.accepts_tip(labware.tip_volume_ul or 0.0):
            raise InvalidArgument(f"Pipette {pipette.name} ({pipette.max_volume_ul:g} µL) does not take {labware.id} tips "
                                  f"({labware.tip_volume_ul:g} µL).")

    def drop(self, pipette: PipetteInfo) -> None:
        self.tips[pipette.name] = Tip()

    # -- liquid -------------------------------------------------------------------------

    def _tip_for_liquid(self, pipette: PipetteInfo) -> Tip:
        tip = self.tip(pipette.name)
        if not tip.attached:
            raise InvalidArgument(f"Pipette {pipette.name} has no tip; call pick_up_tip first.")
        return tip

    def aspirate(self, pipette: PipetteInfo, slot: str, well: str, volume_ul: float) -> None:
        placed = self.get(slot, kinds=LIQUID_KINDS)
        tip = self._tip_for_liquid(pipette)
        if volume_ul < pipette.min_volume_ul - EPS:
            raise InvalidArgument(f"{volume_ul} µL is below pipette {pipette.name}'s minimum of {pipette.min_volume_ul} µL.")
        capacity = tip.capacity_ul or 0.0
        if tip.contents_ul + volume_ul > capacity + EPS:
            raise InvalidArgument(
                f"Pipette {pipette.name} would hold {tip.contents_ul + volume_ul:g} µL; its tip holds at most {capacity:g} µL."
            )
        need = Counter(channel_wells(placed.labware, well, pipette.channels))
        for w, n in need.items():
            have = placed.volumes.get(w)
            if have is not None and have + EPS < n * volume_ul:
                raise InvalidArgument(
                    f"Slot {slot} well {w} holds {have:g} µL; aspirating needs {n * volume_ul:g} µL.",
                    suggested_action="Use another source well, or ask a human to refill it and call load_labware with the new volume.",
                    details={"slot": slot, "well": w, "available_ul": have, "needed_ul": n * volume_ul},
                )
        for w, n in need.items():
            have = placed.volumes.get(w)
            if have is not None:
                placed.volumes[w] = max(0.0, have - n * volume_ul)
        tip.contents_ul += volume_ul

    def dispense(self, pipette: PipetteInfo, slot: str, well: str, volume_ul: float | None) -> float:
        placed = self.get(slot, kinds=LIQUID_KINDS)
        tip = self._tip_for_liquid(pipette)
        volume = tip.contents_ul if volume_ul is None else volume_ul
        if volume <= EPS:
            raise InvalidArgument(f"Pipette {pipette.name}'s tip is empty.")
        if volume > tip.contents_ul + EPS:
            raise InvalidArgument(f"Pipette {pipette.name} holds {tip.contents_ul:g} µL; cannot dispense {volume:g} µL.")
        need = Counter(channel_wells(placed.labware, well, pipette.channels))
        capacity = placed.labware.well_volume_ul
        for w, n in need.items():
            have = placed.volumes.get(w)
            if have is not None and capacity is not None and have + n * volume > capacity + EPS:
                raise InvalidArgument(
                    f"Dispensing {n * volume:g} µL into slot {slot} well {w} ({have:g} µL) would overflow its {capacity:g} µL.",
                    details={"slot": slot, "well": w, "current_ul": have, "capacity_ul": capacity},
                )
        for w, n in need.items():
            have = placed.volumes.get(w)
            if have is not None:
                placed.volumes[w] = have + n * volume
        tip.contents_ul = max(0.0, tip.contents_ul - volume)
        return volume

    # -- reporting -------------------------------------------------------------------------

    def snapshot(self, pipettes: dict[str, PipetteInfo]) -> DeckState:
        slots = []
        for s in self.slots:
            p = self.placed.get(s)
            if p is None:
                slots.append(SlotState(slot=s))
            elif p.labware.kind == "tip_rack":
                slots.append(SlotState(slot=s, labware=p.labware.id, kind=p.labware.kind,
                                       tips_remaining=p.labware.well_count - len(p.used_tips)))
            else:
                slots.append(SlotState(
                    slot=s, labware=p.labware.id, kind=p.labware.kind,
                    volumes_ul={w: round(v, 4) for w, v in p.volumes.items() if v},
                    unknown_volume_wells=sum(v is None for v in p.volumes.values()),
                ))
        states = []
        for info in pipettes.values():
            t = self.tip(info.name)
            states.append(PipetteState(**info.model_dump(), tip_attached=t.attached, tip_capacity_ul=t.capacity_ul,
                                       tip_contents_ul=round(t.contents_ul, 4)))
        return DeckState(slots=slots, pipettes=states)
