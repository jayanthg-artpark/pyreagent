"""Opentrons OT-2 and Flex liquid handlers over the robot's HTTP API.

The driver shares one run per robot with any Opentrons module devices
(``vendors.opentrons.session``). On connect it reads the attached pipettes (and
gripper), the Flex deck configuration (slots, trash bins, staging areas, waste
chute), uploads custom labware definitions, and loads the pipettes into the run.
Each motion is a ``setup`` command, which the robot executes immediately.

What it adds on top of plain pipetting, all from Opentrons' own command set:

- Opentrons' default flow rates per pipette and tip size (``vendors.opentrons.pipettes``)
- blow out (``blowout``), touch tip, move to a well, return tips to their rack
- partial tip pickup (``configureNozzleLayout``): single nozzle and partial column
  on 8-channels, column/row/single on the Flex 96-channel head
- Flex: tip-presence checks after pickup/drop, liquid-level probing, the gripper
  (``moveLabware usingGripper``), the 50 µL low-volume mode (``configureForVolume``)
- deck lights, labware on modules and on Flex staging slots, custom labware

Declare labware by its Opentrons load name (in the pyreagent catalog), map your
own ids with ``load_names``, or pass Opentrons labware JSON in ``labware_definitions``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal, Mapping, Sequence

import httpx

from ....core.errors import InvalidArgument, NeedsAttention
from ....labware import DEFAULT_CATALOG, Labware
from ....labware.opentrons_standard import OPENTRONS_LABWARE
from ....vendors.opentrons.client import DEFAULT_PORT
from ....vendors.opentrons.deck import parse_flex_deck, waste_chute_area
from ....vendors.opentrons.labware import labware_from_definition, load_definition, load_spec
from ....vendors.opentrons.pipettes import LOW_VOLUME, flow_rates, tip_volumes
from ....vendors.opentrons.session import ROBOTS, OpentronsSession
from ..backend import LiquidHandlerBackend
from ..models import NozzleLayout, PipetteInfo
from ..nozzles import nozzle_map, wells_touched

log = logging.getLogger("pyreagent.opentrons")

OPENTRONS_IDS = frozenset(str(item["id"]) for item in OPENTRONS_LABWARE)
USER = "liquid_handler"
BASE_FEATURES = frozenset({"blow_out", "touch_tip", "return_tips", "move_to", "move_labware", "lights"})


def _version(text: str | None) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text or "")[:3])


def nozzle_config(channels: int, layout: NozzleLayout) -> dict[str, Any]:
    """``configureNozzleLayout`` params for a pyreagent nozzle layout."""
    if layout.style == "all":
        return {"style": "ALL"}
    primary = nozzle_map(channels, layout).primary_name
    if layout.style == "partial_column":  # H1 plus the nozzles behind it: Opentrons' H1toG1 ... H1toB1 maps
        back_left = "ABCDEFGH"[8 - (layout.nozzles or 0)] + "1"
        return {"style": "QUADRANT", "primaryNozzle": primary, "frontRightNozzle": primary, "backLeftNozzle": back_left}
    return {"style": layout.style.upper(), "primaryNozzle": primary}


class OpentronsBackend(LiquidHandlerBackend):
    vendor = "Opentrons"
    model = "OT-2 / Flex"

    def __init__(
        self,
        host: str | None = None,
        *,
        robot: Literal["ot2", "flex"],
        port: int = DEFAULT_PORT,
        robot_name: str | None = None,
        trash: str | None = None,
        load_names: Mapping[str, str | Mapping[str, Any]] | None = None,
        labware_definitions: Sequence[str] | None = None,
        pipettes: Mapping[str, Mapping[str, Any]] | None = None,
        aspirate_height_mm: float = 1.0,
        dispense_height_mm: float = 1.0,
        verify_tips: bool = True,
        command_timeout_s: float = 300.0,
        headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Args:
            host: Robot IP address or hostname, or ``"auto"`` to find it on the network (needs zeroconf).
            robot: ``"ot2"`` or ``"flex"``; checked against the robot on connect.
            robot_name: With ``host="auto"``, which robot to use if several are found.
            trash: Addressable area for dropped tips. Default: ``fixedTrash`` on an OT-2; on a Flex the
                first trash bin in the deck configuration, else the waste chute.
            load_names: Map pyreagent labware ids to Opentrons labware: ``"load_name"`` or
                ``{load_name, namespace, version}``.
            labware_definitions: Paths to Opentrons labware JSON (e.g. from Labware Creator). Each is
                added to the pyreagent catalog under its load name and uploaded to every run.
            pipettes: Per-mount overrides, e.g. ``{"left": {"tip_volumes_ul": [20], "default_flow_rate_ul_s": 7.5}}``.
            aspirate_height_mm, dispense_height_mm: Tip height above the well bottom.
            verify_tips: On a Flex, check with the tip sensors that pickups and drops really happened.
            headers: Extra HTTP headers, e.g. an ``Authorization`` token if the robot requires one.
            transport: Inject an httpx transport (tests use a fake robot).
        """
        self.session = OpentronsSession.get(host=host, robot=robot, port=port, robot_name=robot_name, headers=headers, transport=transport)
        self.session.command_timeout_s = command_timeout_s
        self.robot = robot
        self._trash_option = trash
        self.trash = trash or ROBOTS[robot]["trash"]
        trash_slot = self.trash.removeprefix("movableTrash") if robot == "flex" else None
        self.slots = [s for s in ROBOTS[robot]["slots"] if s != trash_slot]
        self.load_names = {k: ({"load_name": v} if isinstance(v, str) else dict(v)) for k, v in (load_names or {}).items()}
        self.custom_definitions = [load_definition(p) for p in (labware_definitions or [])]
        for definition in self.custom_definitions:
            labware = labware_from_definition(definition)
            DEFAULT_CATALOG.add(labware)
            self.load_names.setdefault(labware.id, load_spec(definition))
        self.pipette_overrides = {mount: dict(v) for mount, v in (pipettes or {}).items()}
        self.aspirate_height_mm = aspirate_height_mm
        self.dispense_height_mm = dispense_height_mm
        self.verify_tips = verify_tips
        self.unsupported: list[str] = []
        self.has_gripper = False
        self._pipettes: dict[str, PipetteInfo] = {}
        self._models: dict[str, str] = {}
        self._ids: dict[str, str] = {}
        self._generation = -1
        self._loaded: dict[str, tuple[str, str, dict[str, Any]]] = {}  # slot -> (pyreagent id, robot labwareId, definition)
        self._layouts: dict[str, NozzleLayout] = {}
        self._tip_volume: dict[str, float] = {}
        self._contents: dict[str, float] = {}
        self._low_volume: dict[str, bool] = {}

    # -- features & facts ------------------------------------------------------------------

    @property
    def pipettes(self) -> dict[str, PipetteInfo]:
        return self._pipettes

    @property
    def software_version(self) -> tuple[int, ...]:
        return _version(self.session.health.get("api_version"))

    @property
    def features(self) -> frozenset[str]:  # type: ignore[override]
        features = set(BASE_FEATURES)
        if self.software_version >= (8, 0) and any(p.channels > 1 for p in self._pipettes.values()):
            features.add("nozzle_layouts")
        if self.robot == "flex":
            features.add("liquid_probe")
            if self.has_gripper:
                features.add("gripper")
        return frozenset(features)

    def _load_identity(self, labware: Labware) -> dict[str, Any]:
        spec = self.load_names.get(labware.id)
        if spec is None:
            if labware.id not in OPENTRONS_IDS:
                raise InvalidArgument(
                    f"{labware.id} has no Opentrons load name.",
                    suggested_action="Use Opentrons labware from GET /labware (e.g. opentrons_96_tiprack_300ul), map this id in "
                    "the driver's load_names option, or add its definition JSON to labware_definitions.",
                )
            spec = {"load_name": labware.id}
        return {"loadName": spec["load_name"], "namespace": spec.get("namespace", "opentrons"), "version": int(spec.get("version", 1))}

    def check_placement(self, slot: str, labware: Labware) -> None:
        self._load_identity(labware)
        module = self.session.module_covering(slot)
        if module is not None and slot != module.slot:
            raise InvalidArgument(f"Slot {slot} is covered by the {module.kind} module in slot {module.slot}.")

    # -- connection --------------------------------------------------------------------------

    async def connect(self) -> None:
        await self.session.acquire(USER)
        try:
            await self._sync()
        except BaseException:
            await self.session.release(USER)
            raise

    async def disconnect(self) -> None:
        await self.session.release(USER)

    async def _sync(self) -> None:
        """Load this device's resources into the session's current run."""
        session = self.session
        self._read_instruments(await session.client.instruments())
        if self.robot == "flex":
            deck = parse_flex_deck(session.deck_configuration)
            if self._trash_option is None:
                if deck.trash_bins:
                    self.trash = deck.trash_bins[0]
                elif deck.waste_chute:
                    self.trash = "wasteChute"
                else:
                    raise NeedsAttention("The Flex deck configuration has no trash bin or waste chute.",
                                         suggested_action="Ask a human to configure a trash bin in the Opentrons App, then reset.")
            if deck.slots:
                self.slots = deck.slots
        for definition in self.custom_definitions:
            await session.client.add_labware_definition(session.run_id or "", definition)
        self._ids, self._loaded, self._layouts = {}, {}, {}
        self._tip_volume, self._contents, self._low_volume = {}, {}, {}
        for mount, model in self._models.items():
            result = await session.command("loadPipette", {"pipetteName": model, "mount": mount})
            self._ids[mount] = result["pipetteId"]
        self._generation = session.generation

    def _read_instruments(self, instruments: list[dict[str, Any]]) -> None:
        self._pipettes, self._models, self.unsupported = {}, {}, []
        self.has_gripper = any(i.get("instrumentType") == "gripper" and i.get("ok", True) for i in instruments)
        for item in instruments:
            if item.get("instrumentType") != "pipette" or not item.get("ok", True):
                continue
            mount, model, data = item["mount"], item["instrumentName"], item.get("data", {})
            channels = data.get("channels")
            if channels not in (1, 8, 96):
                self.unsupported.append(f"{mount}: {model} ({channels} channels)")
                continue
            override = self.pipette_overrides.get(mount, {})
            max_ul = float(data["max_volume"])
            min_ul = min(LOW_VOLUME[model].min_ul, float(data["min_volume"])) if model in LOW_VOLUME else float(data["min_volume"])
            rates = flow_rates(model, None)
            self._pipettes[mount] = PipetteInfo(
                name=mount, channels=channels, min_volume_ul=min_ul, max_volume_ul=max_ul,
                default_flow_rate_ul_s=override.get("default_flow_rate_ul_s", rates.aspirate if rates else round(max_ul / 3, 2)),
                tip_volumes_ul=override.get("tip_volumes_ul", tip_volumes(model)),
            )
            self._models[mount] = model
        if self.unsupported:
            log.warning("ignoring unsupported pipettes: %s", ", ".join(self.unsupported))

    async def _command(self, command_type: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if self._generation != self.session.generation:
            await self._sync()  # the run was replaced (stop/reset of another device on this robot)
        return await self.session.command(command_type, params)

    # -- labware ----------------------------------------------------------------------------

    async def _labware(self, slot: str, labware: Labware) -> tuple[str, dict[str, Any]]:
        """The robot's labwareId (and definition) for ``labware`` in ``slot``, loading it on first use."""
        if self._generation != self.session.generation:
            await self._sync()
        loaded = self._loaded.get(slot)
        if loaded is not None and loaded[0] == labware.id:
            return loaded[1], loaded[2]
        if loaded is not None:
            await self._command("moveLabware", {"labwareId": loaded[1], "newLocation": "offDeck", "strategy": "manualMoveWithoutPause"})
            del self._loaded[slot]
        location = await self.session.labware_location(slot)
        result = await self._command("loadLabware", {"location": location, **self._load_identity(labware), "displayName": labware.id})
        self._loaded[slot] = (labware.id, result["labwareId"], result.get("definition") or {})
        return result["labwareId"], self._loaded[slot][2]

    # -- pipette helpers -----------------------------------------------------------------------

    def _pipette_id(self, pipette: PipetteInfo) -> str:
        try:
            return self._ids[pipette.name]
        except KeyError:
            raise InvalidArgument(f"No pipette loaded on mount {pipette.name}.") from None

    def _rate(self, pipette: PipetteInfo, kind: Literal["aspirate", "dispense", "blow_out"], explicit: float | None) -> float:
        if explicit:
            return explicit
        override = self.pipette_overrides.get(pipette.name, {}).get("default_flow_rate_ul_s")
        if override:
            return float(override)
        rates = flow_rates(self._models.get(pipette.name, ""), self._tip_volume.get(pipette.name),
                           low_volume=self._low_volume.get(pipette.name, False))
        return getattr(rates, kind) if rates else pipette.max_volume_ul / 3

    def _tips_checkable(self, pipette: PipetteInfo) -> bool:
        if self.robot != "flex" or not self.verify_tips:
            return False
        layout = self._layouts.get(pipette.name, NozzleLayout())
        if pipette.channels == 8 and len(nozzle_map(8, layout).active) < 4:
            return False  # Opentrons skips presence checks for fewer than 4 tips on an 8-channel
        return not (pipette.channels == 96 and layout.style == "single")

    async def _verify_tips(self, pipette: PipetteInfo, expected: Literal["present", "absent"]) -> None:
        if self._tips_checkable(pipette):
            await self._command("verifyTipPresence", {"pipetteId": self._pipette_id(pipette), "expectedState": expected})

    @staticmethod
    def _location(origin: str, z: float) -> dict[str, Any]:
        return {"origin": origin, "offset": {"x": 0, "y": 0, "z": z}}

    # -- motion ------------------------------------------------------------------------------

    async def home(self) -> None:
        await self._command("home", {})

    async def configure_nozzles(self, *, pipette: PipetteInfo, layout: NozzleLayout) -> None:
        await self._command("configureNozzleLayout", {"pipetteId": self._pipette_id(pipette),
                                                       "configurationParams": nozzle_config(pipette.channels, layout)})
        self._layouts[pipette.name] = layout

    async def pick_up_tips(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str) -> None:
        labware_id, _ = await self._labware(slot, labware)
        await self._command("pickUpTip", {"pipetteId": self._pipette_id(pipette), "labwareId": labware_id, "wellName": well})
        self._tip_volume[pipette.name] = labware.tip_volume_ul or pipette.max_volume_ul
        self._contents[pipette.name] = 0.0
        await self._verify_tips(pipette, "present")

    async def drop_tips(self, *, pipette: PipetteInfo) -> None:
        pipette_id = self._pipette_id(pipette)
        area = waste_chute_area(pipette.channels) if self.trash == "wasteChute" else self.trash
        await self._command("moveToAddressableAreaForDropTip", {"pipetteId": pipette_id, "addressableAreaName": area,
                                                                 "offset": {"x": 0, "y": 0, "z": 0}, "alternateDropLocation": True})
        await self._command("dropTipInPlace", {"pipetteId": pipette_id})
        self._contents[pipette.name] = 0.0
        await self._verify_tips(pipette, "absent")

    async def return_tips(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str) -> None:
        labware_id, _ = await self._labware(slot, labware)
        await self._command("dropTip", {"pipetteId": self._pipette_id(pipette), "labwareId": labware_id, "wellName": well,
                                        "wellLocation": {"origin": "default", "offset": {"x": 0, "y": 0, "z": 0}}})
        self._contents[pipette.name] = 0.0
        await self._verify_tips(pipette, "absent")

    async def _set_volume_mode(self, pipette: PipetteInfo, volume_ul: float) -> None:
        """Flex 50 µL pipettes: switch liquid class around 5 µL, only while the tip is empty."""
        mode = LOW_VOLUME.get(self._models.get(pipette.name, ""))
        if mode is None:
            return
        low = self._low_volume.get(pipette.name, False)
        if self._contents.get(pipette.name, 0.0) <= 1e-6 and (volume_ul < mode.below_ul) != low:
            pipette_id = self._pipette_id(pipette)
            await self._command("configureForVolume", {"pipetteId": pipette_id, "volume": volume_ul})
            await self._command("prepareToAspirate", {"pipetteId": pipette_id})
            self._low_volume[pipette.name] = low = volume_ul < mode.below_ul
        if low and self._contents.get(pipette.name, 0.0) + volume_ul > mode.max_ul + 1e-6:
            raise InvalidArgument(f"In low-volume mode this pipette holds at most {mode.max_ul:g} µL; dispense first.")
        if not low and volume_ul < mode.below_ul:
            raise InvalidArgument(f"Volumes under {mode.below_ul:g} µL need low-volume mode, which can only be set with an empty tip.")

    async def _liquid(self, kind: Literal["aspirate", "dispense"], pipette: PipetteInfo, slot: str, labware: Labware, well: str,
                      volume_ul: float, flow_rate_ul_s: float | None) -> None:
        labware_id, _ = await self._labware(slot, labware)
        height = self.aspirate_height_mm if kind == "aspirate" else self.dispense_height_mm
        await self._command(kind, {
            "pipetteId": self._pipette_id(pipette), "labwareId": labware_id, "wellName": well,
            "wellLocation": self._location("bottom", height), "volume": volume_ul, "flowRate": self._rate(pipette, kind, flow_rate_ul_s),
        })
        sign = 1 if kind == "aspirate" else -1
        self._contents[pipette.name] = max(0.0, self._contents.get(pipette.name, 0.0) + sign * volume_ul)

    async def aspirate(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, volume_ul: float,
                       flow_rate_ul_s: float | None) -> None:
        await self._set_volume_mode(pipette, volume_ul)
        await self._liquid("aspirate", pipette, slot, labware, well, volume_ul, flow_rate_ul_s)

    async def dispense(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, volume_ul: float,
                       flow_rate_ul_s: float | None) -> None:
        await self._liquid("dispense", pipette, slot, labware, well, volume_ul, flow_rate_ul_s)

    async def blow_out(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, flow_rate_ul_s: float | None) -> None:
        labware_id, _ = await self._labware(slot, labware)
        await self._command("blowout", {"pipetteId": self._pipette_id(pipette), "labwareId": labware_id, "wellName": well,
                                        "wellLocation": self._location("top", 0), "flowRate": self._rate(pipette, "blow_out", flow_rate_ul_s)})
        self._contents[pipette.name] = 0.0

    async def touch_tip(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str) -> None:
        labware_id, _ = await self._labware(slot, labware)
        await self._command("touchTip", {"pipetteId": self._pipette_id(pipette), "labwareId": labware_id, "wellName": well})

    async def move_to(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, height_mm: float) -> None:
        labware_id, _ = await self._labware(slot, labware)
        await self._command("moveToWell", {"pipetteId": self._pipette_id(pipette), "labwareId": labware_id, "wellName": well,
                                           "wellLocation": self._location("top", height_mm)})

    async def move_labware(self, *, labware: Labware, from_slot: str, to_slot: str, use_gripper: bool) -> None:
        labware_id, _ = await self._labware(from_slot, labware)
        await self._command("moveLabware", {"labwareId": labware_id, "newLocation": await self.session.labware_location(to_slot),
                                            "strategy": "usingGripper" if use_gripper else "manualMoveWithoutPause"})
        self._loaded[to_slot] = self._loaded.pop(from_slot)

    async def probe_liquid_heights(self, *, pipette: PipetteInfo, slot: str, labware: Labware, wells: list[str]) -> dict[str, float | None]:
        """Liquid surface above each well's bottom, mm: the probe's deck Z minus (well top Z - well depth)."""
        labware_id, definition = await self._labware(slot, labware)
        pipette_id = self._pipette_id(pipette)
        heights: dict[str, float | None] = {}
        for well in wells:
            wells_touched(labware, well, pipette.channels, self._layouts.get(pipette.name, NozzleLayout()))
            depth = (definition.get("wells") or {}).get(well, {}).get("depth")
            top = await self._command("moveToWell", {"pipetteId": pipette_id, "labwareId": labware_id, "wellName": well,
                                                     "wellLocation": self._location("top", 0)})
            await self._command("prepareToAspirate", {"pipetteId": pipette_id})
            result = await self._command("tryLiquidProbe", {"pipetteId": pipette_id, "labwareId": labware_id, "wellName": well,
                                                            "wellLocation": self._location("top", 0)})
            z = result.get("z_position")
            if not isinstance(z, (int, float)):
                heights[well] = None
            elif depth is None:
                raise InvalidArgument(f"The robot's definition of {labware.id} has no depth for {well}; cannot convert to a height.")
            else:
                heights[well] = round(float(z) - (float(top["position"]["z"]) - float(depth)), 2)
        return heights

    async def set_lights(self, on: bool) -> None:
        await self._command("setRailLights", {"on": on})

    async def stop(self) -> None:
        await self.session.stop()

    def info(self) -> dict[str, Any]:
        return {
            **super().info(),
            "robot": self.robot,
            "robot_name": self.session.health.get("name"),
            "software_version": self.session.health.get("api_version"),
            "url": self.session.client.base_url,
            "run_id": self.session.run_id,
            "pipettes": dict(self._models),
            "unsupported_pipettes": list(self.unsupported),
            "gripper": self.has_gripper,
            "trash": self.trash,
            "custom_labware": [d["parameters"]["loadName"] for d in self.custom_definitions],
        }
