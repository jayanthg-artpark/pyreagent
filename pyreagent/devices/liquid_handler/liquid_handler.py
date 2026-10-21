"""Liquid handler capability interface."""

from __future__ import annotations

import asyncio
import math
from collections import Counter
from typing import Annotated, Any, Literal, Mapping

from pydantic import Field

from ...core.capability import capability
from ...core.device import Device
from ...core.errors import InvalidArgument, Interrupted, NotSupported, PyreagentError
from ...labware import DEFAULT_CATALOG, LabwareCatalog
from .backend import LiquidHandlerBackend
from .deck import EPS, LIQUID_KINDS, DeckModel
from .models import DeckState, LiquidHeights, NozzleLayout, PipetteInfo, PipetteState, SlotState, TransferResult
from .nozzles import wells_touched

Slot = Annotated[str, Field(description="Deck slot name, from limits.slots.")]
Well = Annotated[str, Field(description="Well name such as 'A1', under the pipette's primary nozzle (row A of a column for a full 8-channel head).")]
PipetteName = Annotated[
    str | None, Field(description="Pipette name from limits.pipettes. May be omitted only when the robot has a single pipette.")
]
FlowRate = Annotated[float | None, Field(gt=0, description="Flow rate in µL/s; omit for the pipette's default.")]
Wells = Annotated[list[str], Field(min_length=1, max_length=384)]
Nozzles = Annotated[
    NozzleLayout | None,
    Field(description="Partial tip pickup: which nozzles take tips (needs the nozzle_layouts feature). Omit for all nozzles."),
]

# Low-level steps the frontend plans and the backend executes:
#   ("pick_up", slot, well, layout) | ("drop",) | ("return",) | ("aspirate", slot, well, ul)
#   ("dispense", slot, well, ul) | ("blow_out", slot, well) | ("touch_tip", slot, well)
Op = tuple[Any, ...]

_MOTION = {"home", "pick_up_tip", "drop_tip", "return_tip", "aspirate", "dispense", "blow_out", "touch_tip", "mix",
           "transfer", "move_labware", "move_to", "probe_liquid_heights"}
_FEATURE = {"blow_out": "blow_out", "touch_tip": "touch_tip", "return_tip": "return_tips", "move_to": "move_to",
            "probe_liquid_heights": "liquid_probe", "set_lights": "lights"}


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
    ) -> None:
        """
        Args:
            deck: Initial deck layout, ``{slot: labware_id}`` or
                ``{slot: {labware, initial_volume_ul, volumes_ul}}``. A human should
                still check it matches the bench (get_deck shows it).
            max_transfers: Upper bound on source/destination pairs in one transfer.
        """
        super().__init__(name, backend)
        self.labware = labware or DEFAULT_CATALOG
        self.max_transfers = max_transfers
        self._deck = DeckModel(backend.slots, self.labware)
        self._configured: dict[str, NozzleLayout] = {}
        for slot, entry in (deck or {}).items():
            spec = {"labware": entry} if isinstance(entry, str) else dict(entry)
            unknown = set(spec) - {"labware", "initial_volume_ul", "volumes_ul"}
            if unknown or "labware" not in spec:
                raise ValueError(f"deck slot {slot}: expected keys labware, initial_volume_ul, volumes_ul; got {sorted(spec)}")
            try:
                self._deck.place(str(slot), spec["labware"], initial_volume_ul=spec.get("initial_volume_ul"),
                                 volumes_ul=spec.get("volumes_ul"))
                backend.check_placement(str(slot), self.labware.get(spec["labware"]))
            except PyreagentError as err:
                raise ValueError(f"deck slot {slot}: {err.message}") from None

    async def connect(self) -> None:
        await super().connect()
        self._deck.slots = list(self.backend.slots)  # a driver may refine the slot list from the robot
        self._configured = {}

    def limits(self) -> dict[str, Any]:
        return {
            "slots": list(self.backend.slots),
            "pipettes": [p.model_dump(exclude_none=True) for p in self.backend.pipettes.values()],
            "max_transfers": self.max_transfers,
            "features": sorted(self.backend.features),
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

    def _require(self, feature: str) -> None:
        if feature not in self.backend.features:
            raise NotSupported(f"{type(self.backend).__name__} does not support {feature}.",
                               details={"features": sorted(self.backend.features)})

    @staticmethod
    def _tip_op(deck: DeckModel, info: PipetteInfo, slot: str | None, well: str | None, layout: NozzleLayout | None) -> Op:
        layout = layout or NozzleLayout()
        if well is not None and slot is None:
            raise InvalidArgument("Give slot together with well.")
        if well is None:
            slot, well = deck.next_tip(info, slot, layout)
        return ("pick_up", slot, _well(well), layout)

    @staticmethod
    def _apply(deck: DeckModel, info: PipetteInfo, op: Op) -> None:
        kind = op[0]
        if kind == "pick_up":
            deck.pick_up(info, op[1], op[2], op[3])
        elif kind == "drop":
            deck.drop(info)
        elif kind == "return":
            deck.return_tips(info)
        elif kind == "aspirate":
            deck.aspirate(info, op[1], op[2], op[3])
        elif kind == "dispense":
            deck.dispense(info, op[1], op[2], op[3])
        elif kind == "blow_out":
            deck.blow_out(info, op[1], op[2])
        elif kind == "touch_tip":
            deck.touch_tip(info, op[1], op[2])
        else:  # pragma: no cover
            raise ValueError(op)

    async def _run(self, info: PipetteInfo, op: Op, flow_rate_ul_s: float | None = None) -> None:
        """Dry-run ``op`` on the current deck, execute it, then record it."""
        trial = self._deck.copy()
        self._apply(trial, info, op)
        kind, backend = op[0], self.backend
        flow = flow_rate_ul_s  # None: the driver applies its own (tip-aware) default
        if kind == "pick_up":
            layout: NozzleLayout = op[3]
            if self._configured.get(info.name, NozzleLayout()) != layout:
                await backend.configure_nozzles(pipette=info, layout=layout)
                self._configured[info.name] = layout
            await backend.pick_up_tips(pipette=info, slot=op[1], labware=self._deck.placed[op[1]].labware, well=op[2])
        elif kind == "drop":
            await backend.drop_tips(pipette=info)
        elif kind == "return":
            slot, well, _ = self._deck.tip(info.name).origin  # type: ignore[misc]
            await backend.return_tips(pipette=info, slot=slot, labware=self._deck.placed[slot].labware, well=well)
        elif kind in ("aspirate", "dispense"):
            move = backend.aspirate if kind == "aspirate" else backend.dispense
            await move(pipette=info, slot=op[1], labware=self._deck.placed[op[1]].labware, well=op[2], volume_ul=op[3], flow_rate_ul_s=flow)
        elif kind == "blow_out":
            await backend.blow_out(pipette=info, slot=op[1], labware=self._deck.placed[op[1]].labware, well=op[2], flow_rate_ul_s=flow_rate_ul_s)
        elif kind == "touch_tip":
            await backend.touch_tip(pipette=info, slot=op[1], labware=self._deck.placed[op[1]].labware, well=op[2])
        self._apply(self._deck, info, op)

    def _dispense_volume(self, deck: DeckModel, info: PipetteInfo, volume_ul: float | None) -> float:
        return deck.tip(info.name).contents_ul if volume_ul is None else volume_ul

    def _plan_mix(self, deck: DeckModel, info: PipetteInfo, slot: str, well: str, volume_ul: float, cycles: int) -> list[Op]:
        ops: list[Op] = []
        for _ in range(cycles):
            for op in (("aspirate", slot, well, volume_ul), ("dispense", slot, well, volume_ul)):
                self._apply(deck, info, op)
                ops.append(op)
        return ops

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
        layout: NozzleLayout = v["nozzles"] or NozzleLayout()
        if v["new_tip"] == "never":
            layout = deck.tip(info.name).layout
        source_slot, destination_slot, volume = v["source_slot"], v["destination_slot"], v["volume_ul"]
        if source_slot != destination_slot:
            # Whole-transfer totals give clearer errors than the step that runs dry or overflows.
            # Same-slot transfers (serial dilutions) refill their own sources: the step-by-step dry run covers them.
            for slot, wells, verb in ((source_slot, [s for s, _ in pairs], "needs"), (destination_slot, [d for _, d in pairs], "adds")):
                placed = deck.get(slot, kinds=LIQUID_KINDS)
                totals: Counter[str] = Counter()
                for w in wells:
                    for touched in wells_touched(placed.labware, w, info.channels, layout):
                        totals[touched] += volume
                capacity = placed.labware.well_volume_ul
                for w, amount in totals.items():
                    have = placed.volumes.get(w)
                    if have is None:
                        continue
                    if verb == "needs" and have + EPS < amount:
                        raise InvalidArgument(
                            f"Source slot {slot} well {w} holds {have:g} µL; this transfer needs {amount:g} µL from it.",
                            suggested_action="Use more source wells, or ask a human to refill and call load_labware with the new volume.",
                            details={"well": w, "available_ul": have, "needed_ul": amount},
                        )
                    if verb == "adds" and capacity is not None and have + amount > capacity + EPS:
                        raise InvalidArgument(
                            f"Destination slot {slot} well {w} holds {have:g} of {capacity:g} µL; this transfer adds {amount:g} µL.",
                            details={"well": w, "current_ul": have, "adding_ul": amount, "capacity_ul": capacity},
                        )

        ops: list[Op] = []

        def do(op: Op) -> None:
            self._apply(deck, info, op)
            ops.append(op)

        policy = v["new_tip"]
        if policy == "never":
            if not deck.tip(info.name).attached:
                raise InvalidArgument(f"new_tip='never' needs a tip already on pipette {info.name}; call pick_up_tip or use 'once'.")
        elif deck.tip(info.name).attached:
            do(("drop",))
        if policy == "once":
            do(self._tip_op(deck, info, None, None, layout))
        for src, dst in pairs:
            if policy == "always":
                do(self._tip_op(deck, info, None, None, layout))
            tip = deck.tip(info.name)
            room = (tip.capacity_ul or 0.0) - tip.contents_ul
            if room <= EPS:
                raise InvalidArgument(f"Pipette {info.name}'s tip is already full.")
            if v["mix_before_cycles"]:
                for op in self._plan_mix(deck, info, source_slot, src, min(volume, room), v["mix_before_cycles"]):
                    ops.append(op)
            trips = math.ceil(volume / room - EPS)
            for _ in range(trips):
                do(("aspirate", source_slot, src, volume / trips))
                if v["touch_tip"]:
                    do(("touch_tip", source_slot, src))
                do(("dispense", destination_slot, dst, volume / trips))
                if v["touch_tip"]:
                    do(("touch_tip", destination_slot, dst))
            if v["mix_after_cycles"]:
                ops.extend(self._plan_mix(deck, info, destination_slot, dst, min(volume, room), v["mix_after_cycles"]))
            if v["blow_out"]:
                do(("blow_out", destination_slot, dst))
            if policy == "always":
                do(("drop",))
        if policy == "once":
            do(("drop",))
        return len(pairs), ops

    def validate_arguments(self, capability_name: str, values: dict[str, Any]) -> None:
        """Feature checks, then a dry run against a copy of the tracked deck, before any motion."""
        if capability_name in _FEATURE:
            self._require(_FEATURE[capability_name])
        if capability_name == "move_labware":
            self._require("gripper" if values["use_gripper"] else "move_labware")
        if capability_name == "transfer":
            if values["touch_tip"]:
                self._require("touch_tip")
            if values["blow_out"]:
                self._require("blow_out")
        if values.get("nozzles") is not None and values["nozzles"].style != "all":
            self._require("nozzle_layouts")

        deck = self._deck.copy()
        if capability_name == "load_labware":
            deck.place(values["slot"], values["labware"], initial_volume_ul=values["initial_volume_ul"], volumes_ul=values["volumes_ul"])
            self.backend.check_placement(values["slot"], self.labware.get(values["labware"]))
        elif capability_name == "remove_labware":
            deck.remove(values["slot"])
        elif capability_name == "move_labware":
            deck.move(values["from_slot"], values["to_slot"])
            self.backend.check_placement(values["to_slot"], deck.placed[values["to_slot"]].labware)
        elif "pipette" in values:
            info = self._pipette(values["pipette"])
            if capability_name == "pick_up_tip":
                self._apply(deck, info, self._tip_op(deck, info, values["slot"], values["well"], values["nozzles"]))
            elif capability_name == "return_tip":
                deck.return_tips(info)
            elif capability_name in ("aspirate", "touch_tip", "blow_out"):
                op = (("aspirate", values["slot"], _well(values["well"]), values["volume_ul"]) if capability_name == "aspirate"
                      else (capability_name, values["slot"], _well(values["well"])))
                self._apply(deck, info, op)
            elif capability_name == "dispense":
                volume = self._dispense_volume(deck, info, values["volume_ul"])
                self._apply(deck, info, ("dispense", values["slot"], _well(values["well"]), volume))
            elif capability_name == "mix":
                self._plan_mix(deck, info, values["slot"], _well(values["well"]), values["volume_ul"], values["cycles"])
            elif capability_name == "transfer":
                self._plan_transfer(deck, info, values)
            elif capability_name == "probe_liquid_heights":
                deck.check_probe(info, values["slot"], values["wells"])
            elif capability_name == "move_to":
                deck.channel_wells(deck.get(values["slot"]).labware, _well(values["well"]), info)

    # -- deck -----------------------------------------------------------------------------

    @capability(read_only=True)
    async def get_deck(self) -> DeckState:
        """What is in each slot, known liquid volumes, unused tips, and each pipette's tip and nozzle layout.

        Volumes are bookkeeping: what load_labware declared, adjusted by every move
        since. They are not measured (see probe_liquid_heights).
        """
        return self._deck.snapshot(self.backend.pipettes)

    @capability
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
        A wrong declaration makes the robot pipette from, or crash into, the wrong labware.
        """
        self._deck.place(slot, labware, initial_volume_ul=initial_volume_ul, volumes_ul=volumes_ul)
        return self._slot_state(slot)

    @capability
    async def remove_labware(self, slot: Slot) -> SlotState:
        """Record that a slot is now empty because a human took the labware away."""
        self._deck.remove(slot)
        return self._slot_state(slot)

    @capability(long_running=True)
    async def move_labware(
        self,
        from_slot: Slot,
        to_slot: Annotated[str, Field(description="Empty destination slot, from limits.slots.")],
        use_gripper: Annotated[
            bool, Field(description="true: the robot's gripper moves it (Flex). false: a human already moved it; just record the new position.")
        ] = True,
    ) -> DeckState:
        """Move labware, with its tracked liquid and tips, to another deck slot. Runs as a job."""
        self._deck.copy().move(from_slot, to_slot)
        labware = self._deck.placed[from_slot].labware
        await self.backend.move_labware(labware=labware, from_slot=from_slot, to_slot=to_slot, use_gripper=use_gripper)
        self._deck.move(from_slot, to_slot)
        return self._deck.snapshot(self.backend.pipettes)

    # -- motion & tips --------------------------------------------------------------------

    @capability(long_running=True)
    async def home(self) -> None:
        """Move every axis to its home position. Needed after power-up and after a reset."""
        await self.backend.home()

    @capability
    async def pick_up_tip(
        self,
        pipette: PipetteName = None,
        slot: Annotated[str | None, Field(description="Tip rack slot; omit to use the next unused tip anywhere on the deck.")] = None,
        well: Annotated[str | None, Field(description="Tip position under the primary nozzle; omit for the next usable one. Requires slot.")] = None,
        nozzles: Nozzles = None,
    ) -> PipetteState:
        """Pick up fresh tips: one per active nozzle. Each tip is used only once unless returned.

        With a partial nozzle layout the unused nozzles hang over neighbouring tips,
        so those must already be gone; omit well to let pyreagent choose a safe position.
        """
        info = self._pipette(pipette)
        await self._run(info, self._tip_op(self._deck, info, slot, well, nozzles))
        return self._pipette_state(info)

    @capability
    async def drop_tip(self, pipette: PipetteName = None) -> PipetteState:
        """Eject the tips into the trash. Liquid still in them is discarded."""
        info = self._pipette(pipette)
        if self._deck.tip(info.name).attached:
            await self._run(info, ("drop",))
        return self._pipette_state(info)

    @capability
    async def return_tip(self, pipette: PipetteName = None) -> PipetteState:
        """Put the attached tips back where they were picked up; they count as unused again.

        Only reuse a returned tip with the same liquid, or you risk carry-over.
        """
        info = self._pipette(pipette)
        await self._run(info, ("return",))
        return self._pipette_state(info)

    @capability
    async def move_to(
        self,
        slot: Slot,
        well: Well,
        pipette: PipetteName = None,
        height_mm: Annotated[float, Field(ge=0, le=100, description="Height of the tip (or nozzle) above the well's top, mm.")] = 10.0,
    ) -> PipetteState:
        """Move the pipette over a well without handling liquid, e.g. so a person or camera can check alignment."""
        info = self._pipette(pipette)
        labware = self._deck.get(slot).labware
        self._deck.channel_wells(labware, _well(well), info)
        await self.backend.move_to(pipette=info, slot=slot, labware=labware, well=_well(well), height_mm=height_mm)
        return self._pipette_state(info)

    # -- liquid ---------------------------------------------------------------------------

    @capability
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

    @capability
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

    @capability
    async def blow_out(self, slot: Slot, well: Well, pipette: PipetteName = None, flow_rate_ul_s: FlowRate = None) -> PipetteState:
        """Push the last of the liquid out of the tip with air, over a well. The tip is then empty."""
        info = self._pipette(pipette)
        await self._run(info, ("blow_out", slot, _well(well)), flow_rate_ul_s)
        return self._pipette_state(info)

    @capability
    async def touch_tip(self, slot: Slot, well: Well, pipette: PipetteName = None) -> PipetteState:
        """Touch the tip against the sides of a well to knock off hanging droplets."""
        info = self._pipette(pipette)
        await self._run(info, ("touch_tip", slot, _well(well)))
        return self._pipette_state(info)

    @capability(long_running=True)
    async def mix(
        self,
        slot: Slot,
        well: Well,
        volume_ul: Annotated[float, Field(gt=0, description="Volume moved each cycle, per channel, µL.")],
        cycles: Annotated[int, Field(ge=1, le=50, description="Aspirate/dispense cycles.")] = 3,
        pipette: PipetteName = None,
        flow_rate_ul_s: FlowRate = None,
    ) -> PipetteState:
        """Mix a well by aspirating and dispensing repeatedly with the attached tip. Runs as a job."""
        info = self._pipette(pipette)
        ops = self._plan_mix(self._deck.copy(), info, slot, _well(well), volume_ul, cycles)
        for index, op in enumerate(ops, start=1):
            await self._run(info, op, flow_rate_ul_s)
            self.report_progress(index / len(ops), f"cycle {(index + 1) // 2}/{cycles}")
        return self._pipette_state(info)

    @capability(long_running=True)
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
        nozzles: Nozzles = None,
        mix_before_cycles: Annotated[int, Field(ge=0, le=20, description="Mix the source this many times before aspirating.")] = 0,
        mix_after_cycles: Annotated[int, Field(ge=0, le=20, description="Mix each destination this many times after dispensing.")] = 0,
        touch_tip: Annotated[bool, Field(description="Touch the tip to the well sides after each aspirate and dispense.")] = False,
        blow_out: Annotated[bool, Field(description="Blow out into each destination after its last dispense.")] = False,
    ) -> TransferResult:
        """Move a volume from source wells to destination wells, handling tips, mixing and large volumes.

        The whole plan is checked against the tracked deck first (tips, liquid in the
        sources, room in the destinations), so an impossible transfer is refused before
        anything moves. Runs as a job.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        info = self._pipette(pipette)
        values = dict(source_slot=source_slot, source_wells=source_wells, destination_slot=destination_slot,
                      destination_wells=destination_wells, volume_ul=volume_ul, new_tip=new_tip, nozzles=nozzles,
                      mix_before_cycles=mix_before_cycles, mix_after_cycles=mix_after_cycles, touch_tip=touch_tip, blow_out=blow_out)
        pairs, ops = self._plan_transfer(self._deck.copy(), info, values)
        for index, op in enumerate(ops, start=1):
            await self._run(info, op)
            self.report_progress(index / len(ops), f"step {index}/{len(ops)}: {op[0]}")
        return TransferResult(transfers=pairs, volume_ul=volume_ul, tips_used=sum(op[0] == "pick_up" for op in ops),
                              operations=len(ops), duration_s=round(loop.time() - started, 3))

    @capability(long_running=True)
    async def probe_liquid_heights(
        self, slot: Slot, wells: Annotated[Wells, Field(description="Wells to probe, one at a time.")], pipette: PipetteName = None
    ) -> LiquidHeights:
        """Measure the liquid surface in wells with the pipette's liquid sensor (Opentrons Flex).

        Needs an empty tip attached. Heights are mm above each well's bottom; null
        means no liquid was found. The tip has touched liquid afterwards. Runs as a job.
        """
        info = self._pipette(pipette)
        checked = self._deck.check_probe(info, slot, wells)
        heights = await self.backend.probe_liquid_heights(pipette=info, slot=slot, labware=self._deck.placed[slot].labware, wells=checked)
        return LiquidHeights(slot=slot, heights_mm=heights)

    # -- robot ------------------------------------------------------------------------------

    @capability
    async def set_lights(self, on: Annotated[bool, Field(description="true to switch the deck lights on.")]) -> None:
        """Switch the robot's deck lights on or off."""
        await self.backend.set_lights(on)

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
        # Whoever reset the device removed any tips from the pipettes by hand first.
        self._deck.tips.clear()
        self._configured = {}
