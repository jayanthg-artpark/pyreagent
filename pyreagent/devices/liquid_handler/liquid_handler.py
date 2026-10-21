"""Liquid handler capability interface."""

from __future__ import annotations

import asyncio
import math
from collections import Counter
from typing import Annotated, Any, Literal, Mapping

from pydantic import Field

from ...core.capability import capability
from ...core.device import Device
from ...core.errors import InvalidArgument, Interrupted, PyreagentError
from ...labware import DEFAULT_CATALOG, LabwareCatalog
from .backend import LiquidHandlerBackend
from .deck import EPS, LIQUID_KINDS, DeckModel, channel_wells
from .models import DeckState, PipetteInfo, PipetteState, SlotState, TransferResult

Slot = Annotated[str, Field(description="Deck slot name, from limits.slots.")]
Well = Annotated[str, Field(description="Well name such as 'A1'. For an 8-channel pipette: the well under the first channel (row A).")]
PipetteName = Annotated[
    str | None, Field(description="Pipette name from limits.pipettes. May be omitted only when the robot has a single pipette.")
]
FlowRate = Annotated[float | None, Field(gt=0, description="Flow rate in µL/s; omit for the pipette's default.")]
Wells = Annotated[list[str], Field(min_length=1, max_length=384)]

# Low-level steps the frontend plans and the backend executes:
#   ("pick_up", slot, well) | ("drop",) | ("aspirate", slot, well, ul) | ("dispense", slot, well, ul)
Op = tuple[Any, ...]

_MOTION = {"home", "pick_up_tip", "drop_tip", "aspirate", "dispense", "transfer"}


def _well(name: str) -> str:
    return name.strip().upper()


class LiquidHandler(Device[LiquidHandlerBackend]):
    """Pipetting robot with a deck of labware slots and one or more pipettes."""

    device_type = "liquid_handler"

    def __init__(
        self,
        name: str,
        backend: LiquidHandlerBackend,
        *,
        deck: Mapping[str, str | Mapping[str, Any]] | None = None,
        labware: LabwareCatalog | None = None,
        max_transfers: int = 384,
        confirm: Mapping[str, bool] | None = None,
    ) -> None:
        """
        Args:
            deck: Initial deck layout, ``{slot: labware_id}`` or
                ``{slot: {labware, initial_volume_ul, volumes_ul}}``. A human should
                still confirm it matches the bench (get_deck shows it).
            max_transfers: Upper bound on source/destination pairs in one transfer.
        """
        super().__init__(name, backend, confirm=confirm)
        self.labware = labware or DEFAULT_CATALOG
        self.max_transfers = max_transfers
        self._deck = DeckModel(backend.slots, self.labware)
        for slot, entry in (deck or {}).items():
            spec = {"labware": entry} if isinstance(entry, str) else dict(entry)
            unknown = set(spec) - {"labware", "initial_volume_ul", "volumes_ul"}
            if unknown or "labware" not in spec:
                raise ValueError(f"deck slot {slot}: expected keys labware, initial_volume_ul, volumes_ul; got {sorted(spec)}")
            try:
                self._deck.place(str(slot), spec["labware"], initial_volume_ul=spec.get("initial_volume_ul"),
                                 volumes_ul=spec.get("volumes_ul"))
            except PyreagentError as err:
                raise ValueError(f"deck slot {slot}: {err.message}") from None

    def limits(self) -> dict[str, Any]:
        return {
            "slots": list(self.backend.slots),
            "pipettes": [p.model_dump(exclude_none=True) for p in self.backend.pipettes.values()],
            "max_transfers": self.max_transfers,
        }

    # -- helpers -----------------------------------------------------------------------

    def _pipette(self, name: str | None) -> PipetteInfo:
        pipettes = self.backend.pipettes
        if name is None:
            if len(pipettes) == 1:
                return next(iter(pipettes.values()))
            raise InvalidArgument("This robot has several pipettes; pass pipette.", details={"pipettes": sorted(pipettes)})
        try:
            return pipettes[name]
        except KeyError:
            raise InvalidArgument(f"No pipette {name!r}.", details={"pipettes": sorted(pipettes)}) from None

    def _pipette_state(self, info: PipetteInfo) -> PipetteState:
        return self._deck.snapshot({info.name: info}).pipettes[0]

    def _slot_state(self, slot: str) -> SlotState:
        return next(s for s in self._deck.snapshot({}).slots if s.slot == slot)

    @staticmethod
    def _tip_op(deck: DeckModel, info: PipetteInfo, slot: str | None, well: str | None) -> Op:
        if well is not None and slot is None:
            raise InvalidArgument("Give slot together with well.")
        if well is None:
            slot, well = deck.next_tip(info, slot)
        return ("pick_up", slot, _well(well))

    @staticmethod
    def _apply(deck: DeckModel, info: PipetteInfo, op: Op) -> None:
        kind = op[0]
        if kind == "pick_up":
            deck.pick_up(info, op[1], op[2])
        elif kind == "drop":
            deck.drop(info)
        elif kind == "aspirate":
            deck.aspirate(info, op[1], op[2], op[3])
        elif kind == "dispense":
            deck.dispense(info, op[1], op[2], op[3])
        else:  # pragma: no cover
            raise ValueError(op)

    async def _run(self, info: PipetteInfo, op: Op, flow_rate_ul_s: float | None = None) -> None:
        """Dry-run ``op`` on the current deck, execute it, then record it."""
        self._apply(self._deck.copy(), info, op)
        kind = op[0]
        if kind == "pick_up":
            await self.backend.pick_up_tips(pipette=info, slot=op[1], labware=self._deck.placed[op[1]].labware, well=op[2])
        elif kind == "drop":
            await self.backend.drop_tips(pipette=info)
        else:
            move = self.backend.aspirate if kind == "aspirate" else self.backend.dispense
            await move(pipette=info, slot=op[1], labware=self._deck.placed[op[1]].labware, well=op[2], volume_ul=op[3],
                       flow_rate_ul_s=flow_rate_ul_s or info.default_flow_rate_ul_s)
        self._apply(self._deck, info, op)

    def _dispense_volume(self, deck: DeckModel, info: PipetteInfo, volume_ul: float | None) -> float:
        return deck.tip(info.name).contents_ul if volume_ul is None else volume_ul

    def _plan_transfer(self, deck: DeckModel, info: PipetteInfo, v: dict[str, Any]) -> tuple[int, list[Op]]:
        sources = [_well(w) for w in v["source_wells"]]
        destinations = [_well(w) for w in v["destination_wells"]]
        if len(sources) == len(destinations):
            pairs = list(zip(sources, destinations))
        elif len(sources) == 1:
            pairs = [(sources[0], d) for d in destinations]
        elif len(destinations) == 1:
            pairs = [(s, destinations[0]) for s in sources]
        else:
            raise InvalidArgument(
                f"Got {len(sources)} sources and {len(destinations)} destinations. Use equal-length lists, "
                "one source for many destinations, or many sources into one destination."
            )
        if len(pairs) > self.max_transfers:
            raise InvalidArgument(f"{len(pairs)} transfers exceeds limits.max_transfers ({self.max_transfers}); split the work.")
        source_slot = v["source_slot"]
        if source_slot != v["destination_slot"]:
            # Whole-transfer demand per source well gives a clearer error than the step that runs dry.
            # Same-slot transfers (serial dilutions) refill their sources, so only the step-by-step dry run applies.
            placed = deck.get(source_slot, kinds=LIQUID_KINDS)
            demand: Counter[str] = Counter()
            for src, _ in pairs:
                for w in channel_wells(placed.labware, src, info.channels):
                    demand[w] += v["volume_ul"]
            for w, needed in demand.items():
                have = placed.volumes.get(w)
                if have is not None and have + EPS < needed:
                    raise InvalidArgument(
                        f"Source slot {source_slot} well {w} holds {have:g} µL; this transfer needs {needed:g} µL from it.",
                        suggested_action="Use more source wells, or ask a human to refill and call load_labware with the new volume.",
                        details={"well": w, "available_ul": have, "needed_ul": needed},
                    )
            target = deck.get(v["destination_slot"], kinds=LIQUID_KINDS)
            incoming: Counter[str] = Counter()
            for _, dst in pairs:
                for w in channel_wells(target.labware, dst, info.channels):
                    incoming[w] += v["volume_ul"]
            capacity = target.labware.well_volume_ul
            for w, added in incoming.items():
                have = target.volumes.get(w)
                if have is not None and capacity is not None and have + added > capacity + EPS:
                    raise InvalidArgument(
                        f"Destination slot {v['destination_slot']} well {w} holds {have:g} of {capacity:g} µL; "
                        f"this transfer adds {added:g} µL.",
                        details={"well": w, "current_ul": have, "adding_ul": added, "capacity_ul": capacity},
                    )

        ops: list[Op] = []

        def do(op: Op) -> None:
            self._apply(deck, info, op)
            ops.append(op)

        policy, volume = v["new_tip"], v["volume_ul"]
        if policy == "never":
            if not deck.tip(info.name).attached:
                raise InvalidArgument(f"new_tip='never' needs a tip already on pipette {info.name}; call pick_up_tip or use 'once'.")
        elif deck.tip(info.name).attached:
            do(("drop",))
        if policy == "once":
            do(self._tip_op(deck, info, None, None))
        for src, dst in pairs:
            if policy == "always":
                do(self._tip_op(deck, info, None, None))
            tip = deck.tip(info.name)
            room = (tip.capacity_ul or 0.0) - tip.contents_ul
            if room <= EPS:
                raise InvalidArgument(f"Pipette {info.name}'s tip is already full.")
            trips = math.ceil(volume / room - EPS)
            for _ in range(trips):
                do(("aspirate", v["source_slot"], src, volume / trips))
                do(("dispense", v["destination_slot"], dst, volume / trips))
            mix_volume = min(volume, room)
            for _ in range(v["mix_after_cycles"]):
                do(("aspirate", v["destination_slot"], dst, mix_volume))
                do(("dispense", v["destination_slot"], dst, mix_volume))
            if policy == "always":
                do(("drop",))
        if policy == "once":
            do(("drop",))
        return len(pairs), ops

    def validate_arguments(self, capability_name: str, values: dict[str, Any]) -> None:
        """Dry-run the call against a copy of the tracked deck, before any approval or motion."""
        deck = self._deck.copy()
        if capability_name == "load_labware":
            deck.place(values["slot"], values["labware"], initial_volume_ul=values["initial_volume_ul"], volumes_ul=values["volumes_ul"])
        elif capability_name == "remove_labware":
            deck.remove(values["slot"])
        elif capability_name in ("pick_up_tip", "drop_tip", "aspirate", "dispense", "transfer"):
            info = self._pipette(values["pipette"])
            if capability_name == "pick_up_tip":
                self._apply(deck, info, self._tip_op(deck, info, values["slot"], values["well"]))
            elif capability_name == "aspirate":
                self._apply(deck, info, ("aspirate", values["slot"], _well(values["well"]), values["volume_ul"]))
            elif capability_name == "dispense":
                volume = self._dispense_volume(deck, info, values["volume_ul"])
                self._apply(deck, info, ("dispense", values["slot"], _well(values["well"]), volume))
            elif capability_name == "transfer":
                self._plan_transfer(deck, info, values)

    # -- capabilities ---------------------------------------------------------------------

    @capability(read_only=True)
    async def get_deck(self) -> DeckState:
        """What is in each slot, known liquid volumes, unused tips, and each pipette's tip.

        Volumes are bookkeeping: what load_labware declared, adjusted by every move
        since. They are not measured.
        """
        return self._deck.snapshot(self.backend.pipettes)

    @capability(
        requires_confirmation=True,
        hazard="Declares what is physically in a deck slot. If it is wrong the robot pipettes from, or crashes into, the wrong labware.",
    )
    async def load_labware(
        self,
        slot: Slot,
        labware: Annotated[str, Field(description="Labware id from GET /labware (list_labware).")],
        initial_volume_ul: Annotated[
            float | None,
            Field(ge=0, description="Liquid in every well, µL. 0 for an empty plate; omit if unknown (volume checks are then skipped)."),
        ] = None,
        volumes_ul: Annotated[
            dict[str, Annotated[float, Field(ge=0)]] | None,
            Field(description="Per-well volumes in µL overriding initial_volume_ul, e.g. {'A1': 1500}."),
        ] = None,
    ) -> SlotState:
        """Record that a human placed labware in a slot, replacing what was recorded there.

        Call again after replacing a tip rack (resets tip tracking) or refilling a reservoir.
        """
        self._deck.place(slot, labware, initial_volume_ul=initial_volume_ul, volumes_ul=volumes_ul)
        return self._slot_state(slot)

    @capability
    async def remove_labware(self, slot: Slot) -> SlotState:
        """Record that a slot is now empty because a human took the labware away."""
        self._deck.remove(slot)
        return self._slot_state(slot)

    @capability(long_running=True)
    async def home(self) -> None:
        """Move every axis to its home position. Needed after power-up and after a reset."""
        await self.backend.home()

    @capability
    async def pick_up_tip(
        self,
        pipette: PipetteName = None,
        slot: Annotated[str | None, Field(description="Tip rack slot; omit to use the next unused tip anywhere on the deck.")] = None,
        well: Annotated[str | None, Field(description="Tip position; omit for the next unused one. Requires slot.")] = None,
    ) -> PipetteState:
        """Pick up a fresh tip (a column of 8 for an 8-channel pipette). Each tip is used only once."""
        info = self._pipette(pipette)
        await self._run(info, self._tip_op(self._deck, info, slot, well))
        return self._pipette_state(info)

    @capability
    async def drop_tip(self, pipette: PipetteName = None) -> PipetteState:
        """Eject the tip into the trash. Liquid still in it is discarded."""
        info = self._pipette(pipette)
        if self._deck.tip(info.name).attached:
            await self._run(info, ("drop",))
        return self._pipette_state(info)

    @capability(requires_confirmation=True, hazard="Draws liquid (reagent or sample) out of a well.")
    async def aspirate(
        self,
        slot: Slot,
        well: Well,
        volume_ul: Annotated[float, Field(gt=0, description="Volume per channel, µL.")],
        pipette: PipetteName = None,
        flow_rate_ul_s: FlowRate = None,
    ) -> PipetteState:
        """Draw liquid into the attached tip. Prefer transfer, which also handles tips and splitting."""
        info = self._pipette(pipette)
        await self._run(info, ("aspirate", slot, _well(well), volume_ul), flow_rate_ul_s)
        return self._pipette_state(info)

    @capability(requires_confirmation=True, hazard="Dispenses the liquid held in the tip into a well.")
    async def dispense(
        self,
        slot: Slot,
        well: Well,
        volume_ul: Annotated[float | None, Field(gt=0, description="Volume per channel, µL; omit to dispense everything in the tip.")] = None,
        pipette: PipetteName = None,
        flow_rate_ul_s: FlowRate = None,
    ) -> PipetteState:
        """Dispense liquid from the attached tip into a well."""
        info = self._pipette(pipette)
        volume = self._dispense_volume(self._deck, info, volume_ul)
        await self._run(info, ("dispense", slot, _well(well), volume), flow_rate_ul_s)
        return self._pipette_state(info)

    @capability(long_running=True, requires_confirmation=True, hazard="Moves liquid from the source wells into the destination wells.")
    async def transfer(
        self,
        source_slot: Slot,
        source_wells: Annotated[Wells, Field(description="Source wells: one for many destinations, many into one destination, or a list paired with destination_wells.")],
        destination_slot: Slot,
        destination_wells: Annotated[Wells, Field(description="Destination wells, matched to source_wells as described there.")],
        volume_ul: Annotated[float, Field(gt=0, description="Volume per pair, per channel, µL. More than a tip holds is split into several trips.")],
        pipette: PipetteName = None,
        new_tip: Annotated[
            Literal["always", "once", "never"],
            Field(description="always: fresh tip for every pair (no carry-over). once: one tip for the whole transfer. never: keep using the tip already attached."),
        ] = "always",
        mix_after_cycles: Annotated[int, Field(ge=0, le=20, description="Mix by aspirating/dispensing this many times in each destination.")] = 0,
    ) -> TransferResult:
        """Move a volume from source wells to destination wells, handling tips and large volumes.

        The whole plan is checked against the tracked deck first (tips, liquid in the
        sources, room in the destinations), so an impossible transfer is refused before
        anything moves. Runs as a job.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        info = self._pipette(pipette)
        values = dict(source_slot=source_slot, source_wells=source_wells, destination_slot=destination_slot,
                      destination_wells=destination_wells, volume_ul=volume_ul, new_tip=new_tip, mix_after_cycles=mix_after_cycles)
        pairs, ops = self._plan_transfer(self._deck.copy(), info, values)
        for index, op in enumerate(ops, start=1):
            await self._run(info, op)
            self.report_progress(index / len(ops), f"step {index}/{len(ops)}: {op[0]}")
        return TransferResult(transfers=pairs, volume_ul=volume_ul, tips_used=sum(op[0] == "pick_up" for op in ops),
                              operations=len(ops), duration_s=round(loop.time() - started, 3))

    @capability(interrupt=True)
    async def stop(self) -> DeckState:
        """Halt all motion now. Safe at any time.

        If something was running, the robot may be stopped holding liquid, so the
        device goes to error state: a human must check it, remove any tips by hand,
        and then call reset.
        """
        if self._activity is not None:
            self._fault(Interrupted(f"Stopped during {self._activity.capability}.", details=self._activity.model_dump(mode="json")))
            self.cancel_activity()
        await self.backend.stop()
        return self._deck.snapshot(self.backend.pipettes)

    async def on_cancel(self, capability_name: str) -> None:
        if capability_name in _MOTION:
            await self.backend.stop()
            self._fault(Interrupted(f"{capability_name} was cancelled mid-motion; a human must inspect the deck and tips, then reset."))

    async def on_reset(self) -> None:
        # The human removed any tips by hand before approving the reset.
        self._deck.tips.clear()
