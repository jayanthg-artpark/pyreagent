"""Deck bookkeeping: what is in each slot, liquid per well, used tips, what each tip holds.

Every operation validates first and only then mutates, so running an operation
on a ``copy()`` is a dry run. The frontend uses that to refuse impossible plans
(no tips left, source too empty, destination would overflow) before a job is
created, and again right before each motion.

Which wells a pipette touches depends on its nozzle layout (``nozzles.py``):
the layout is chosen when tips are picked up and applies until they are dropped.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ...core.errors import InvalidArgument, NeedsAttention
from ...labware import Labware, LabwareCatalog
from .models import DeckState, NozzleLayout, PipetteInfo, PipetteState, SlotState
from .nozzles import tip_shadow, wells_touched

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
    origin: tuple[str, str, tuple[str, ...]] | None = None
    """(slot, well, tip wells) the attached tips came from, so they can be returned."""
    layout: NozzleLayout = field(default_factory=NozzleLayout)


class DeckModel:
    def __init__(self, slots: list[str], catalog: LabwareCatalog) -> None:
        self.slots = list(slots)
        self.catalog = catalog
        self.placed: dict[str, Placed] = {}
        self.tips: dict[str, Tip] = {}

    def copy(self) -> "DeckModel":
        clone = DeckModel(self.slots, self.catalog)
        clone.placed = {s: Placed(p.labware, dict(p.volumes), set(p.used_tips)) for s, p in self.placed.items()}
        clone.tips = {n: Tip(t.attached, t.capacity_ul, t.contents_ul, t.origin, t.layout) for n, t in self.tips.items()}
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

    def move(self, from_slot: str, to_slot: str) -> None:
        """Labware (with its tracked liquid and tips) moves to an empty slot."""
        self.get(from_slot)
        self._check_slot(to_slot)
        if to_slot in self.placed:
            raise InvalidArgument(f"Slot {to_slot} already holds {self.placed[to_slot].labware.id}.")
        if any(t.attached and t.origin and t.origin[0] == from_slot for t in self.tips.values()):
            raise InvalidArgument(f"A pipette holds tips from slot {from_slot}; drop or return them before moving the rack.")
        self.placed[to_slot] = self.placed.pop(from_slot)

    # -- addressing ---------------------------------------------------------------------

    def channel_wells(self, labware: Labware, well: str, pipette: PipetteInfo) -> list[str]:
        """Wells under the pipette's active nozzles (repeats for a trough shared by several nozzles)."""
        return wells_touched(labware, well, pipette.channels, self.tip(pipette.name).layout)

    # -- tips -------------------------------------------------------------------------

    def tip(self, pipette: str) -> Tip:
        return self.tips.setdefault(pipette, Tip())

    @staticmethod
    def _check_tips_fit(pipette: PipetteInfo, labware: Labware) -> None:
        if not pipette.accepts_tip(labware.tip_volume_ul or 0.0):
            raise InvalidArgument(f"Pipette {pipette.name} ({pipette.max_volume_ul:g} µL) does not take {labware.id} tips "
                                  f"({labware.tip_volume_ul:g} µL).")

    def _pickup_problem(self, pipette: PipetteInfo, placed: Placed, well: str, layout: NozzleLayout) -> str | None:
        """Why a pickup at ``well`` is impossible, or None. Raises for positions the head cannot take at all."""
        wells = wells_touched(placed.labware, well, pipette.channels, layout)
        used = sorted(placed.used_tips.intersection(wells))
        if used:
            return f"tip(s) {', '.join(used)} were already used"
        blocked = [w for w in tip_shadow(placed.labware, well, pipette.channels, layout) if w not in placed.used_tips]
        if blocked:
            return (f"the unused nozzles would land on tips at {', '.join(blocked[:6])}{'...' if len(blocked) > 6 else ''}; "
                    "use those tips first, or let pyreagent choose the position")
        return None

    def next_tip(self, pipette: PipetteInfo, slot: str | None = None, layout: NozzleLayout | None = None) -> tuple[str, str]:
        """First position (column by column) where the layout's active nozzles all find unused tips.

        Without ``slot``: racks of compatible tips, smallest tip size first, then in slot order.
        """
        layout = layout or NozzleLayout()
        if slot is not None:
            candidates = [slot]
            self._check_tips_fit(pipette, self.get(slot, kinds=("tip_rack",)).labware)
        else:
            racks = [(p.labware.tip_volume_ul or 0.0, self.slots.index(s), s) for s, p in self.placed.items()
                     if p.labware.kind == "tip_rack" and pipette.accepts_tip(p.labware.tip_volume_ul or 0.0) and s in self.slots]
            candidates = [s for _, _, s in sorted(racks)]
        for s in candidates:
            placed = self.placed[s]
            for well in placed.labware.wells_by_column():
                try:
                    if self._pickup_problem(pipette, placed, well, layout) is None:
                        return s, well
                except InvalidArgument:
                    continue
        raise NeedsAttention(
            f"No unused tips left for pipette {pipette.name}"
            + (f" with the {layout.style} layout" if layout.style != "all" else "") + (f" in slot {slot}." if slot else "."),
            suggested_action="Ask a human to replace the tip rack, then call load_labware for that slot to reset tip tracking.",
        )

    def pick_up(self, pipette: PipetteInfo, slot: str, well: str, layout: NozzleLayout | None = None) -> None:
        layout = layout or NozzleLayout()
        tip = self.tip(pipette.name)
        if tip.attached:
            raise InvalidArgument(f"Pipette {pipette.name} already has a tip; call drop_tip first.")
        placed = self.get(slot, kinds=("tip_rack",))
        self._check_tips_fit(pipette, placed.labware)
        problem = self._pickup_problem(pipette, placed, well, layout)
        if problem:
            raise InvalidArgument(f"Cannot pick up at slot {slot} {well}: {problem}.")
        wells = wells_touched(placed.labware, well, pipette.channels, layout)
        placed.used_tips.update(wells)
        tip.attached, tip.capacity_ul, tip.contents_ul = True, min(placed.labware.tip_volume_ul or 0.0, pipette.max_volume_ul), 0.0
        tip.origin, tip.layout = (slot, well, tuple(wells)), layout

    def drop(self, pipette: PipetteInfo) -> None:
        self.tips[pipette.name] = Tip(layout=self.tip(pipette.name).layout)

    def return_tips(self, pipette: PipetteInfo) -> tuple[str, str]:
        """Put the attached tips back where they came from; they count as unused again. Returns (slot, well)."""
        tip = self.tip(pipette.name)
        if not tip.attached or tip.origin is None:
            raise InvalidArgument(f"Pipette {pipette.name} has no tip to return.")
        if tip.contents_ul > EPS:
            raise InvalidArgument(f"Pipette {pipette.name} still holds {tip.contents_ul:g} µL; dispense or blow out first.")
        slot, well, wells = tip.origin
        placed = self.placed.get(slot)
        if placed is None or placed.labware.kind != "tip_rack":
            raise InvalidArgument(f"The tip rack these tips came from is no longer in slot {slot}.")
        placed.used_tips.difference_update(wells)
        self.drop(pipette)
        return slot, well

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
        need = Counter(self.channel_wells(placed.labware, well, pipette))
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
        self._add(placed, slot, self.channel_wells(placed.labware, well, pipette), volume)
        tip.contents_ul = max(0.0, tip.contents_ul - volume)
        return volume

    @staticmethod
    def _add(placed: Placed, slot: str, wells: list[str], volume: float) -> None:
        need = Counter(wells)
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

    def blow_out(self, pipette: PipetteInfo, slot: str, well: str) -> None:
        """Whatever is left in the tip ends up in the well."""
        placed = self.get(slot, kinds=LIQUID_KINDS)
        tip = self._tip_for_liquid(pipette)
        if tip.contents_ul > EPS:
            self._add(placed, slot, self.channel_wells(placed.labware, well, pipette), tip.contents_ul)
        tip.contents_ul = 0.0

    def touch_tip(self, pipette: PipetteInfo, slot: str, well: str) -> None:
        placed = self.get(slot, kinds=LIQUID_KINDS)
        self._tip_for_liquid(pipette)
        self.channel_wells(placed.labware, well, pipette)

    def check_probe(self, pipette: PipetteInfo, slot: str, wells: list[str]) -> list[str]:
        placed = self.get(slot, kinds=LIQUID_KINDS)
        tip = self._tip_for_liquid(pipette)
        if tip.contents_ul > EPS:
            raise InvalidArgument("Liquid probing needs an empty tip; dispense or blow out first.")
        checked = placed.labware.check_wells(wells)
        for w in checked:
            self.channel_wells(placed.labware, w, pipette)
        return checked

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
            states.append(PipetteState(**info.model_dump(), layout=t.layout, tip_attached=t.attached, tip_capacity_ul=t.capacity_ul,
                                       tip_contents_ul=round(t.contents_ul, 4)))
        return DeckState(slots=slots, pipettes=states)
