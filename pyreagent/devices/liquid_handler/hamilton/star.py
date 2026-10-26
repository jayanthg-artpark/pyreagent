"""Hamilton Microlab STAR / STARlet (independent channels, CO-RE 96 head, iSWAP) over its USB firmware.

Protocol knowledge comes from PyLabRobot's reverse-engineered STAR backend (MIT); this is an
independent implementation in pyreagent's driver model. Command strings reproduce the ones
PyLabRobot's tests expect (see ``tests/drivers/test_hamilton_star.py``).

Deck: carriers on tracks, configured as ``carriers = {name = {type, track}}``; slots are
"<carrier>.<site>" with sites numbered 1.. from the front. Tip racks must be Hamilton CO-RE racks
(``hamilton_96_tiprack_*``); plates and reservoirs can be any labware with geometry.

Pipettes: "channels" (8 independent channels; layouts choose how many, from the back) and, when
installed, "core96" (the CO-RE 96 head, whole racks and plates only).

Liquid handling uses Hamilton's water liquid classes (volume correction, flows, air gaps). With
``blow_out`` (default) the tip takes blow-out air on aspirate and is emptied with it when a
dispense empties it. Liquid is aspirated ``bottom_clearance_mm`` above the well bottom unless
``lld`` turns on liquid level detection, which follows the surface.

Not supported: 4- and 16-channel STARs, autoload, CO-RE grippers, plate lids, the right arm,
aborting a firmware command in flight (``stop`` cannot interrupt a move).
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Mapping

from ....core.errors import InvalidArgument, NeedsAttention, NotSupported, PyreagentError
from ....io import UsbBulkTransport
from ....labware import DEFAULT_CATALOG, Labware
from ....vendors.hamilton.deck import HAMILTON_LABWARE, HAMILTON_TIPS, TRACKS, TRASH96_A1, HamiltonTip, StarDeck
from ....vendors.hamilton.firmware import (NO_LIQUID, STAR_PRODUCT_ID, VENDOR_ID, PacketTransport, StarFirmware,
                                           channel_list, channel_module, ints, pad)
from ....vendors.hamilton.liquid_classes import LiquidClass, water_class
from ..backend import LiquidHandlerBackend
from ..models import NozzleLayout, PipetteInfo
from ..nozzles import wells_touched
from ..positions import ChannelTarget, Site, channel_targets, require_spacing

log = logging.getLogger("pyreagent.hamilton")

CHANNELS, CORE96 = "channels", "core96"
MIN_SPACING_MM = 9.0
LLD_MODES = {"off": 0, "capacitive": 1, "pressure": 2}
Z_DRIVE_MM_PER_STEP = 0.01072765
TIME_LONG = 300.0


class HamiltonStarBackend(LiquidHandlerBackend):
    vendor = "Hamilton"
    model = "Microlab STAR"

    def __init__(
        self,
        *,
        carriers: Mapping[str, Mapping[str, Any]],
        model: Literal["starlet", "star"] = "starlet",
        serial_number: str | None = None,
        traverse_height_mm: float = 245.0,
        iswap_traverse_height_mm: float = 280.0,
        bottom_clearance_mm: float = 1.0,
        lld: Literal["off", "capacitive", "pressure"] = "off",
        lld_immersion_mm: float = 2.0,
        dispense_mode: Literal["surface", "jet"] = "surface",
        blow_out: bool = True,
        iswap_grip_from_top_mm: float = 5.0,
        iswap_grip_strength: int = 4,
        timeout_s: float = 60.0,
        transport: PacketTransport | None = None,
    ) -> None:
        """
        Args:
            carriers: ``{"tips": {"type": "TIP_CAR_480_A00", "track": 1}, ...}``; slots become "tips.1".."tips.5".
            model: "starlet" (30 tracks) or "star" (54 tracks); checked against the instrument on connect.
            bottom_clearance_mm: aspirate/dispense height above the well bottom when LLD is off.
            lld: liquid level detection for aspirates (conductive tips for "capacitive").
            dispense_mode: "surface" touches the liquid; "jet" dispenses from above it.
            blow_out: take blow-out air on aspirate and blow it out when a dispense empties the tip.
            iswap_grip_from_top_mm: how far below a plate's top the iSWAP grips it.
        """
        self.deck = StarDeck(model, {name: (str(c["type"]), int(c.get("track", c.get("rail", 0)))) for name, c in carriers.items()})
        self.slots = self.deck.slots
        for labware in HAMILTON_LABWARE:
            DEFAULT_CATALOG.add(labware)
        self.firmware = StarFirmware(transport or UsbBulkTransport(VENDOR_ID, STAR_PRODUCT_ID, serial_number=serial_number,
                                                                   read_size=4096), timeout_s=timeout_s)
        self.traverse = round(traverse_height_mm * 10)
        self.iswap_traverse = round(iswap_traverse_height_mm * 10)
        self.bottom_clearance_mm = bottom_clearance_mm
        self.lld_mode = LLD_MODES[lld]
        self.lld_immersion_mm = lld_immersion_mm
        self.jet = dispense_mode == "jet"
        self.blow_out = blow_out
        self.iswap_grip_from_top_mm = iswap_grip_from_top_mm
        self.iswap_grip_strength = iswap_grip_strength
        self.channels = 8
        self.iswap_present = self.core96_present = False
        self.tip_waste_x = 0
        self._layout = NozzleLayout()
        self._tip_types: dict[str, int] = {}
        self._tips: dict[int, HamiltonTip] = {}
        self._class: dict[int, LiquidClass | None] = {}
        self._in_tip: dict[int, float] = {}
        """Per channel: plunger volume (µL, after correction) still in the tip."""
        self._liquid: dict[int, float] = {}
        self._tips96: HamiltonTip | None = None
        self._class96: LiquidClass | None = None
        self._in_tip96 = self._liquid96 = 0.0

    # -- facts --------------------------------------------------------------------------------

    @property
    def pipettes(self) -> dict[str, PipetteInfo]:
        tips = [10.0, 50.0, 300.0, 1000.0]
        pipettes = {CHANNELS: PipetteInfo(name=CHANNELS, channels=8, min_volume_ul=0.5, max_volume_ul=1000.0, tip_volumes_ul=tips,
                                          independent_channels=True)}
        if self.core96_present:
            pipettes[CORE96] = PipetteInfo(name=CORE96, channels=96, min_volume_ul=0.5, max_volume_ul=1000.0, tip_volumes_ul=tips)
        return pipettes

    @property
    def features(self) -> frozenset[str]:  # type: ignore[override]
        features = {"return_tips", "move_to", "nozzle_layouts", "move_labware", "liquid_probe"}
        if self.iswap_present:
            features.add("gripper")
        return frozenset(features)

    def check_placement(self, slot: str, labware: Labware) -> None:
        self.deck.check(slot, labware)

    # -- connection ----------------------------------------------------------------------------

    async def _send(self, module: str, command: str, params: list[tuple[str, Any]] | None = None, *,
                    timeout_s: float | None = None) -> dict[str, str]:
        return await self.firmware.send(module, command, params or [], timeout_s=timeout_s)

    async def connect(self) -> None:
        await self.firmware.transport.open()
        try:
            await self._initialise()
        except BaseException:
            await self.firmware.transport.close()
            raise

    async def _initialise(self) -> None:
        machine = await self._send("C0", "RM")
        self.channels = int(machine.get("kp", "8"))
        if self.channels != 8:
            raise NeedsAttention(f"This STAR has {self.channels} channels; the pyreagent STAR driver supports 8.")
        self.firmware.channels = self.channels
        extended = await self._send("C0", "QM")
        left_arm = int(extended.get("xl", "0"), 16)
        self.iswap_present = bool(left_arm & 0b10)
        self.core96_present = bool(left_arm & 0b100)
        self.tip_waste_x = int(extended.get("xw", "0"))
        tracks = int(extended.get("xt", "0"))
        if tracks and tracks != TRACKS[self.deck.model]:
            raise NeedsAttention(f"The instrument has {tracks} tracks but the driver is configured as {self.deck.model} "
                                 f"({TRACKS[self.deck.model]} tracks); the trash and reachable area would be wrong.",
                                 suggested_action="Set model to " + ("star" if tracks == 54 else "starlet") + " in the driver options.")
        initialised = (await self._send("C0", "QW")).get("qw") == "1"
        if not initialised:
            await self._send("C0", "VI", timeout_s=TIME_LONG)
        else:
            await self._send("C0", "ZA")
        tips = ints((await self._send("C0", "RT")).get("rt", ""))
        if not initialised or any(tips):
            # parks the channels over the tip waste, ejecting any tips left on them
            dy = (4050 - 2175) // (self.channels - 1)
            all_channels = {ch: "1" for ch in range(self.channels)}
            await self._send("C0", "DI", [
                ("xp", channel_list({ch: pad(self.tip_waste_x, 5) for ch in all_channels}, self.channels)),
                ("yp", [pad(4050 - ch * dy, 4) for ch in range(self.channels)]),
                ("tp", pad(self.traverse, 4)), ("tz", "1220"), ("te", "3600"),
                ("tm", channel_list(all_channels, self.channels)), ("tt", "04"), ("ti", "0"),
            ], timeout_s=120)
        if self.iswap_present:
            if (await self._send("R0", "QW")).get("qw") != "1":
                await self._send("C0", "FI", timeout_s=TIME_LONG)
            await self._park_iswap()
        if self.core96_present and (await self._send("H0", "QW")).get("qw") != "1":
            await self._send("C0", "EI", [*self._xs(TRASH96_A1[0]), ("yh", pad(TRASH96_A1[1] * 10, 4)),
                                          ("za", pad(TRASH96_A1[2] * 10, 4)), ("ze", pad(self.traverse, 4))], timeout_s=TIME_LONG)
        self._tip_types = {}
        self._tips, self._class, self._in_tip, self._liquid = {}, {}, {}, {}
        self._tips96, self._class96, self._in_tip96, self._liquid96 = None, None, 0.0, 0.0

    async def disconnect(self) -> None:
        await self.firmware.transport.close()

    # -- helpers -------------------------------------------------------------------------------

    @staticmethod
    def _xs(x_mm: float) -> list[tuple[str, str]]:
        return [("xs", pad(abs(x_mm) * 10, 5)), ("xd", "0" if x_mm >= 0 else "1")]

    def _per(self, values: Mapping[int, float], width: int, *, zero: bool = False) -> list[str]:
        """Per-channel list; ``zero`` pads unused positions with zeros (positions), else with the first value."""
        return channel_list({ch: pad(v, width) for ch, v in values.items()}, self.channels, "0" * width if zero else None)

    def _pattern(self, used: list[int]) -> list[str]:
        return channel_list({ch: "1" for ch in used}, self.channels, "0")

    def _site(self, slot: str, labware: Labware) -> Site:
        return self.deck.site(slot, labware)

    def _targets(self, slot: str, labware: Labware, well: str) -> list[ChannelTarget]:
        targets = channel_targets(self._site(slot, labware), labware, wells_touched(labware, well, 8, self._layout))
        if len({t.x_mm for t in targets}) != 1:
            raise InvalidArgument("The STAR channels share one x arm; all wells must be in one column.")
        require_spacing(targets, MIN_SPACING_MM)
        return targets

    async def _tip_type(self, rack_id: str) -> int:
        """Firmware tip-type index for a rack's tips, defined with C0TT on first use."""
        if rack_id not in self._tip_types:
            index = len(self._tip_types) + 1
            index += index >= 14  # the firmware reserves 14 for the CO-RE gripper tool
            tip = HAMILTON_TIPS[rack_id]
            await self._send("C0", "TT", [("tt", f"{index:02d}"), ("tf", "1" if tip.has_filter else "0"),
                                          ("tl", pad((tip.length_mm - tip.fitting_depth_mm) * 10, 4)),
                                          ("tv", pad(tip.max_volume_ul * 10, 5)), ("tg", str(tip.size)), ("tu", "0")])
            self._tip_types[rack_id] = index
        return self._tip_types[rack_id]

    def _tip(self, labware: Labware) -> HamiltonTip:
        try:
            return HAMILTON_TIPS[labware.id]
        except KeyError:
            raise InvalidArgument(f"{labware.id} is not a Hamilton tip rack.", suggested_action=f"Use one of {sorted(HAMILTON_TIPS)}.") from None

    # -- channels: tips --------------------------------------------------------------------------

    async def home(self) -> None:
        await self._send("C0", "ZA")
        if self.iswap_present:
            await self._park_iswap()

    async def configure_nozzles(self, *, pipette: PipetteInfo, layout: NozzleLayout) -> None:
        if pipette.name == CORE96:
            if layout.style != "all":
                raise NotSupported("The CO-RE 96 head picks up whole racks only.")
            return
        self._layout = layout  # channels are independent: a layout only chooses how many are used

    async def pick_up_tips(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str) -> None:
        tip = self._tip(labware)
        index = await self._tip_type(labware.id)
        if pipette.name == CORE96:
            return await self._pick_up_tips96(slot, labware, well, tip, index)
        targets = self._targets(slot, labware, well)
        used = [t.channel for t in targets]
        spot_z = max(t.bottom_z_mm for t in targets)
        await self._send("C0", "TP", [
            ("xp", self._per({t.channel: t.x_mm * 10 for t in targets}, 5, zero=True)),
            ("yp", self._per({t.channel: t.y_mm * 10 for t in targets}, 4, zero=True)),
            ("tm", self._pattern(used)), ("tt", f"{index:02d}"),
            ("tp", pad((spot_z + tip.collar_mm) * 10, 4)), ("tz", pad(spot_z * 10, 4)),
            ("th", pad(self.traverse, 4)), ("td", "0"),
        ], timeout_s=120)
        for ch in used:
            self._tips[ch], self._class[ch], self._in_tip[ch], self._liquid[ch] = tip, None, 0.0, 0.0

    def _forget_tips(self) -> None:
        self._tips, self._class, self._in_tip, self._liquid = {}, {}, {}, {}

    async def drop_tips(self, *, pipette: PipetteInfo) -> None:
        if pipette.name == CORE96:
            x, y, z = TRASH96_A1
            await self._send("C0", "ER", [*self._xs(x), ("yh", pad(y * 10, 4)), ("za", pad(z * 10, 4)),
                                          ("zh", pad(self.traverse, 4)), ("ze", pad(self.traverse, 4))], timeout_s=120)
            self._tips96 = None
            return
        used = sorted(self._tips) or list(range(self.channels))
        x, y, z = self.deck.trash
        top = y + (len(used) - 1) / 2 * MIN_SPACING_MM
        await self._send("C0", "TR", [
            ("xp", self._per({ch: x * 10 for ch in used}, 5, zero=True)),
            ("yp", self._per({ch: (top - i * MIN_SPACING_MM) * 10 for i, ch in enumerate(used)}, 4, zero=True)),
            ("tm", self._pattern(used)), ("tp", pad((z + 59.9) * 10, 4)), ("tz", pad((z + 49.9) * 10, 4)),
            ("th", pad(self.traverse, 4)), ("te", pad(self.traverse, 4)), ("ti", "0"),
        ], timeout_s=120)
        self._forget_tips()

    async def return_tips(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str) -> None:
        tip = self._tip(labware)
        if pipette.name == CORE96:
            x, y, z = self._a1_96(slot, labware, well)
            await self._send("C0", "ER", [*self._xs(x), ("yh", pad(y * 10, 4)), ("za", pad(z * 10, 4)),
                                          ("zh", pad(self.traverse, 4)), ("ze", pad(self.traverse, 4))], timeout_s=120)
            self._tips96 = None
            return
        targets = self._targets(slot, labware, well)
        spot_z = max(t.bottom_z_mm for t in targets)
        await self._send("C0", "TR", [
            ("xp", self._per({t.channel: t.x_mm * 10 for t in targets}, 5, zero=True)),
            ("yp", self._per({t.channel: t.y_mm * 10 for t in targets}, 4, zero=True)),
            ("tm", self._pattern([t.channel for t in targets])),
            ("tp", pad((spot_z + tip.collar_mm) * 10, 4)), ("tz", pad((spot_z + tip.collar_mm - tip.fitting_depth_mm) * 10, 4)),
            ("th", pad(self.traverse, 4)), ("te", pad(self.traverse, 4)), ("ti", "1"),
        ], timeout_s=120)
        self._forget_tips()

    # -- channels: liquid ------------------------------------------------------------------------

    def _class_for(self, ch: int) -> LiquidClass | None:
        tip = self._tips.get(ch)
        if tip is None:
            return None
        return water_class(tip.volume_ul, tip.has_filter, self.jet, self.blow_out)

    def _heights(self, labware: Labware, bottom_z: float) -> tuple[float, float]:
        """(liquid height to work at, LLD search start) in mm."""
        geometry = labware.require_geometry()
        return bottom_z + self.bottom_clearance_mm, bottom_z + geometry.well_depth_mm + 2.7

    async def aspirate(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, volume_ul: float,
                       flow_rate_ul_s: float | None) -> None:
        if pipette.name == CORE96:
            return await self._aspirate96(slot, labware, well, volume_ul, flow_rate_ul_s)
        targets = self._targets(slot, labware, well)
        used = [t.channel for t in targets]
        lcs = {ch: self._class.get(ch) or self._class_for(ch) for ch in used}
        work, search = {}, {}
        for t in targets:
            work[t.channel], search[t.channel] = self._heights(labware, t.bottom_z_mm)
        bottom = {t.channel: t.bottom_z_mm + self.bottom_clearance_mm for t in targets}
        volume = {ch: lc.corrected(volume_ul) if lc else volume_ul for ch, lc in lcs.items()}
        blow = {ch: (lc.aspirate_blow_out if lc and self.blow_out and not self._in_tip.get(ch) else 0.0) for ch, lc in lcs.items()}
        same = lambda value, width: self._per({ch: value for ch in used}, width)  # noqa: E731
        by = lambda attr, default, scale, width: self._per(  # noqa: E731
            {ch: (getattr(lc, attr) if lc else default) * scale for ch, lc in lcs.items()}, width)
        await self._send("C0", "AS", [
            ("at", same(0, 1)), ("tm", self._pattern(used)),
            ("xp", self._per({t.channel: t.x_mm * 10 for t in targets}, 5, zero=True)),
            ("yp", self._per({t.channel: t.y_mm * 10 for t in targets}, 4, zero=True)),
            ("th", pad(self.traverse, 4)), ("te", pad(self.traverse, 4)),
            ("lp", self._per({ch: v * 10 for ch, v in search.items()}, 4)), ("ch", by("clot_retract_mm", 0, 10, 3)),
            ("zl", self._per({ch: v * 10 for ch, v in work.items()}, 4)), ("po", same(100, 4)), ("zu", same(32, 4)),
            ("zr", same(6180, 5)), ("zx", self._per({ch: v * 10 for ch, v in bottom.items()}, 4)),
            ("ip", same(self.lld_immersion_mm * 10 if self.lld_mode else 0, 4)), ("it", same(0, 1)), ("fp", same(0, 4)),
            ("av", self._per({ch: v * 10 for ch, v in volume.items()}, 5)),
            ("as", self._per({ch: (flow_rate_ul_s or (lc.aspirate_flow if lc else 100.0)) * 10 for ch, lc in lcs.items()}, 4)),
            ("ta", by("aspirate_transport_air", 0, 10, 3)), ("ba", self._per({ch: v * 10 for ch, v in blow.items()}, 4)),
            ("oa", same(0, 3)), ("lm", same(self.lld_mode, 1)), ("ll", same(1, 1)), ("lv", same(1, 1)), ("zo", same(0, 3)),
            ("ld", same(0, 2)), ("de", by("aspirate_swap_speed", 100, 10, 4)), ("wt", by("aspirate_settling_s", 0, 10, 2)),
            ("mv", same(0, 5)), ("mc", same(0, 2)), ("mp", same(0, 3)), ("ms", same(1000, 4)), ("mh", same(0, 4)),
            ("gi", same(0, 3)), ("gj", "0"), ("gk", "0"), ("lk", same(0, 1)), ("ik", same(0, 4)), ("sd", same(500, 4)),
            ("se", same(500, 4)), ("sz", same(300, 4)), ("io", same(0, 4)),
        ], timeout_s=TIME_LONG)
        for ch in used:
            self._class[ch] = lcs[ch]
            self._in_tip[ch] = self._in_tip.get(ch, 0.0) + volume[ch]
            self._liquid[ch] = self._liquid.get(ch, 0.0) + volume_ul

    async def dispense(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, volume_ul: float,
                       flow_rate_ul_s: float | None) -> None:
        if pipette.name == CORE96:
            return await self._dispense96(slot, labware, well, volume_ul, flow_rate_ul_s)
        targets = self._targets(slot, labware, well)
        used = [t.channel for t in targets]
        lcs = {ch: self._class.get(ch) or self._class_for(ch) for ch in used}
        empties = {ch: volume_ul >= self._liquid.get(ch, 0.0) - 1e-6 for ch in used}
        volume, mode, blow = {}, {}, {}
        for ch, lc in lcs.items():
            if empties[ch]:
                volume[ch] = self._in_tip.get(ch, 0.0) or (lc.corrected(volume_ul) if lc else volume_ul)
            else:
                volume[ch] = lc.corrected(volume_ul) if lc else volume_ul
            emptying = empties[ch] and self.blow_out
            mode[ch] = (1 if emptying else 0) if self.jet else (3 if emptying else 2)
            blow[ch] = lc.dispense_blow_out if lc and emptying else 0.0
        work, search = {}, {}
        for t in targets:
            work[t.channel], search[t.channel] = self._heights(labware, t.bottom_z_mm)
        same = lambda value, width: self._per({ch: value for ch in used}, width)  # noqa: E731
        by = lambda attr, default, scale, width: self._per(  # noqa: E731
            {ch: (getattr(lc, attr) if lc else default) * scale for ch, lc in lcs.items()}, width)
        await self._send("C0", "DS", [
            ("dm", self._per(mode, 1)), ("tm", self._pattern(used)),
            ("xp", self._per({t.channel: t.x_mm * 10 for t in targets}, 5, zero=True)),
            ("yp", self._per({t.channel: t.y_mm * 10 for t in targets}, 4, zero=True)),
            ("zx", self._per({ch: v * 10 for ch, v in work.items()}, 4)),
            ("lp", self._per({ch: v * 10 for ch, v in search.items()}, 4)),
            ("zl", self._per({ch: v * 10 for ch, v in work.items()}, 4)), ("po", same(100, 4)), ("ip", same(0, 4)),
            ("it", same(0, 1)), ("fp", same(0, 4)), ("zu", same(32, 4)), ("zr", same(6180, 5)),
            ("th", pad(self.traverse, 4)), ("te", pad(self.traverse, 4)),
            ("dv", self._per({ch: v * 10 for ch, v in volume.items()}, 5)),
            ("ds", self._per({ch: (flow_rate_ul_s or (lc.dispense_flow if lc else 120.0)) * 10 for ch, lc in lcs.items()}, 4)),
            ("ss", same(50, 4)), ("rv", by("stop_back_ul", 0, 10, 3)), ("ta", by("dispense_transport_air", 0, 10, 3)),
            ("ba", self._per({ch: v * 10 for ch, v in blow.items()}, 4)), ("lm", same(0, 1)), ("dj", "00"), ("zo", same(0, 3)),
            ("ll", same(1, 1)), ("lv", same(1, 1)), ("de", by("dispense_swap_speed", 10, 10, 4)),
            ("wt", by("dispense_settling_s", 0, 10, 2)), ("mv", same(0, 5)), ("mc", same(0, 2)), ("mp", same(0, 3)),
            ("ms", same(10, 4)), ("mh", same(0, 4)), ("gi", same(0, 3)), ("gj", "0"), ("gk", "0"),
        ], timeout_s=TIME_LONG)
        for ch in used:
            if empties[ch]:
                self._in_tip[ch], self._liquid[ch] = 0.0, 0.0
            else:
                self._in_tip[ch] = max(self._in_tip.get(ch, 0.0) - volume[ch], 0.0)
                self._liquid[ch] = self._liquid.get(ch, 0.0) - volume_ul

    # -- channels: moves and liquid detection ------------------------------------------------------

    def _channel_ys(self, assigned: Mapping[int, float]) -> list[float]:
        """Y (mm) of every channel: ``assigned`` where given, the rest packed 9 mm apart around them."""
        ys: list[float] = []
        first = min(assigned)
        for ch in range(self.channels):
            if ch in assigned:
                ys.append(assigned[ch])
            elif ch < first:
                ys.append(assigned[first] + (first - ch) * MIN_SPACING_MM)
            else:
                ys.append(ys[-1] - MIN_SPACING_MM)
        if ys[0] > 650 or ys[-1] < 6:
            raise InvalidArgument("The channels cannot all fit between the back and front of the deck for these wells.")
        return ys

    async def _position(self, x_mm: float, assigned: Mapping[int, float]) -> None:
        await self._send("C0", "ZA")
        await self._send("C0", "KX", [("xs", pad(x_mm * 10, 5))])
        await self._send("C0", "JY", [("yp", " ".join(pad(y * 10, 4) for y in self._channel_ys(assigned)))])

    async def move_to(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, height_mm: float) -> None:
        if pipette.name == CORE96:
            raise NotSupported("move_to is available for the independent channels only.")
        targets = self._targets(slot, labware, well)
        top = self._site(slot, labware).top(labware)
        await self._position(targets[0].x_mm, {t.channel: t.y_mm for t in targets})
        for t in targets:
            await self._send("C0", "KZ", [("pn", f"{t.channel + 1:02d}"), ("zj", pad((top + height_mm) * 10, 4))])

    async def probe_liquid_heights(self, *, pipette: PipetteInfo, slot: str, labware: Labware, wells: list[str]) -> dict[str, float | None]:
        """Capacitive LLD with the channels that carry tips (conductive tips needed); one column at a time."""
        if pipette.name == CORE96:
            raise NotSupported("Liquid probing uses the independent channels.")
        tipped = sorted(self._tips)
        if not tipped:
            raise InvalidArgument("Pick up (conductive) tips on the channels first; they detect the liquid.")
        site = self._site(slot, labware)
        geometry = labware.require_geometry()
        positions = {w: site.well(labware, w) for w in labware.check_wells(wells)}
        results: dict[str, float | None] = {}
        pending = sorted(positions, key=lambda w: (positions[w][0], -positions[w][1]))
        while pending:
            x = positions[pending[0]][0]
            batch: dict[int, str] = {}
            last_y: float | None = None
            for w in [w for w in pending if positions[w][0] == x]:
                if len(batch) == len(tipped):
                    break
                y = positions[w][1]
                if last_y is None or last_y - y >= MIN_SPACING_MM - 1e-6:
                    batch[tipped[len(batch)]] = w
                    last_y = y
            await self._position(x, {ch: positions[w][1] for ch, w in batch.items()})
            found: dict[int, bool] = {}
            for ch, w in batch.items():
                tip = self._tips[ch]
                above_nozzle = tip.length_mm - tip.fitting_depth_mm
                bottom = positions[w][2]
                lowest = max(round((bottom + above_nozzle) / Z_DRIVE_MM_PER_STEP), 9320)
                start = min(round((bottom + geometry.well_depth_mm + above_nozzle + 5) / Z_DRIVE_MM_PER_STEP), 31200)
                try:
                    await self._send(channel_module(ch), "ZL", [("zh", f"{lowest:05d}"), ("zc", f"{start:05d}"), ("zl", "00932"),
                                                                ("zr", "075"), ("gt", "0010"), ("gl", "0002"), ("zj", "1"),
                                                                ("zi", "0186")], timeout_s=120)
                    found[ch] = True
                except PyreagentError as err:
                    traces = {e["trace"] for e in err.details.get("errors", {}).values()}
                    if not traces & NO_LIQUID:
                        raise
                    found[ch] = False
            levels = ints((await self._send("C0", "RL")).get("lh", ""))
            for ch, w in batch.items():
                results[w] = round(levels[ch] / 10 - positions[w][2], 2) if found[ch] and ch < len(levels) else None
            pending = [w for w in pending if w not in batch.values()]
        await self._send("C0", "ZA")
        return results

    # -- CO-RE 96 head ------------------------------------------------------------------------------

    def _a1_96(self, slot: str, labware: Labware, well: str) -> tuple[float, float, float]:
        """Where the head's A1 nozzle goes for ``well`` under it (troughs: the head is centred)."""
        x, y, z = self._site(slot, labware).well(labware, well)
        if labware.columns == 1:
            x -= 5.5 * 9
        if labware.rows == 1:
            y += 3.5 * 9
        return x, y, z

    async def _pick_up_tips96(self, slot: str, labware: Labware, well: str, tip: HamiltonTip, index: int) -> None:
        x, y, z = self._a1_96(slot, labware, well)
        # dispensing drive to its lowest position without tips, as the tips are pressed on
        await self._send("H0", "DQ", [("dq", "11281"), ("dv", "13500"), ("du", "00000"), ("dr", "900000"), ("dw", "15")])
        await self._send("C0", "EP", [*self._xs(x), ("yh", pad(y * 10, 4)), ("tt", f"{index:02d}"), ("wu", "0"),
                                      ("za", pad(z * 10, 4)), ("zh", pad(self.traverse, 4)), ("ze", pad(self.traverse, 4))],
                         timeout_s=120)
        self._tips96, self._class96, self._in_tip96, self._liquid96 = tip, None, 0.0, 0.0

    def _common96(self, labware: Labware, z: float) -> tuple[float, float]:
        geometry = labware.require_geometry()
        return z + self.bottom_clearance_mm, z + geometry.well_depth_mm + 2.7

    async def _aspirate96(self, slot: str, labware: Labware, well: str, volume_ul: float, flow: float | None) -> None:
        tip = self._tips96
        lc = self._class96 or (water_class(tip.volume_ul, tip.has_filter, self.jet, self.blow_out, core96=True) if tip else None)
        x, y, z = self._a1_96(slot, labware, well)
        work, search = self._common96(labware, z)
        volume = lc.corrected(volume_ul) if lc else volume_ul
        blow = lc.aspirate_blow_out if lc and self.blow_out and not self._in_tip96 else 0.0
        await self._send("C0", "EA", [
            ("aa", "0"), *self._xs(x), ("yh", pad(y * 10, 4)), ("zh", pad(self.traverse, 4)), ("ze", pad(self.traverse, 4)),
            ("lz", pad(search * 10, 4)), ("zt", pad(work * 10, 4)), ("pp", "0100"), ("zm", pad(work * 10, 4)), ("zv", "0032"),
            ("zq", "06180"), ("iw", pad(self.lld_immersion_mm * 10 if self.lld_mode else 0, 3)), ("ix", "0"), ("fh", "000"),
            ("af", pad(volume * 10, 5)), ("ag", pad((flow or (lc.aspirate_flow if lc else 250.0)) * 10, 4)),
            ("vt", pad((lc.aspirate_transport_air if lc else 0) * 10, 3)), ("bv", pad(blow * 10, 5)), ("wv", "00000"),
            ("cm", "1" if self.lld_mode else "0"), ("cs", "1"), ("bs", pad((lc.aspirate_swap_speed if lc else 100) * 10, 4)),
            ("wh", pad((lc.aspirate_settling_s if lc else 0.5) * 10, 2)), ("hv", "00000"), ("hc", "00"), ("hp", "000"),
            ("mj", "000"), ("hs", "1200"), ("cw", "F" * 24), ("cr", "000"), ("cj", "0"), ("cx", "0"),
        ], timeout_s=TIME_LONG)
        self._class96 = lc
        self._in_tip96 += volume
        self._liquid96 += volume_ul

    async def _dispense96(self, slot: str, labware: Labware, well: str, volume_ul: float, flow: float | None) -> None:
        tip = self._tips96
        lc = self._class96 or (water_class(tip.volume_ul, tip.has_filter, self.jet, self.blow_out, core96=True) if tip else None)
        x, y, z = self._a1_96(slot, labware, well)
        work, search = self._common96(labware, z)
        empties = volume_ul >= self._liquid96 - 1e-6
        emptying = empties and self.blow_out
        volume = (self._in_tip96 or (lc.corrected(volume_ul) if lc else volume_ul)) if empties else (lc.corrected(volume_ul) if lc else volume_ul)
        mode = (1 if emptying else 0) if self.jet else (3 if emptying else 2)
        await self._send("C0", "ED", [
            ("da", str(mode)), *self._xs(x), ("yh", pad(y * 10, 4)), ("zm", pad(work * 10, 4)), ("zv", "0032"), ("zq", "06180"),
            ("lz", pad(search * 10, 4)), ("zt", pad(work * 10, 4)), ("pp", "0100"), ("iw", "000"), ("ix", "0"), ("fh", "000"),
            ("zh", pad(self.traverse, 4)), ("ze", pad(self.traverse, 4)), ("df", pad(volume * 10, 5)),
            ("dg", pad((flow or (lc.dispense_flow if lc else 120.0)) * 10, 4)), ("es", "0050"),
            ("ev", pad((lc.stop_back_ul if lc else 0) * 10, 3)), ("vt", pad((lc.dispense_transport_air if lc else 0) * 10, 3)),
            ("bv", pad((lc.dispense_blow_out if lc and emptying else 0) * 10, 5)), ("cm", "0"), ("cs", "1"), ("ej", "00"),
            ("bs", pad((lc.dispense_swap_speed if lc else 10) * 10, 4)), ("wh", pad((lc.dispense_settling_s if lc else 0) * 10, 2)),
            ("hv", "00000"), ("hc", "00"), ("hp", "000"), ("mj", "000"), ("hs", "1200"), ("cw", "F" * 24), ("cr", "000"),
            ("cj", "0"), ("cx", "0"),
        ], timeout_s=TIME_LONG)
        if empties:
            self._in_tip96, self._liquid96 = 0.0, 0.0
        else:
            self._in_tip96 = max(self._in_tip96 - volume, 0.0)
            self._liquid96 -= volume_ul

    # -- iSWAP ----------------------------------------------------------------------------------------

    async def _park_iswap(self) -> None:
        await self._send("C0", "PG", [("th", pad(self.iswap_traverse, 4))], timeout_s=120)

    def _grip_point(self, slot: str, labware: Labware) -> tuple[float, float, float]:
        site = self._site(slot, labware)
        geometry = labware.require_geometry()
        return (site.x_mm + geometry.size_x_mm / 2, site.y_mm + geometry.size_y_mm / 2,
                site.z_mm + geometry.size_z_mm - self.iswap_grip_from_top_mm)

    async def move_labware(self, *, labware: Labware, from_slot: str, to_slot: str, use_gripper: bool) -> None:
        if not use_gripper:
            return  # a human moved it; the STAR keeps no labware state of its own
        if labware.kind == "tip_rack":
            raise NotSupported("The iSWAP moves plates; tip racks stay on their carrier.")
        width = labware.require_geometry().size_x_mm  # gripped from the front, across x
        sx, sy, sz = self._grip_point(from_slot, labware)
        dx, dy, dz = self._grip_point(to_slot, labware)
        common = [("th", pad(self.iswap_traverse, 4)), ("te", pad(self.iswap_traverse, 4))]
        await self._send("C0", "PP", [*self._xs(sx), ("yj", pad(sy * 10, 4)), ("yd", "0"), ("zj", pad(sz * 10, 4)), ("zd", "0"),
                                      ("gr", "1"), *common, ("gw", str(self.iswap_grip_strength)), ("go", pad((width + 3) * 10, 4)),
                                      ("gb", pad(width * 10 - 33, 4)), ("gt", "20"), ("ga", "0"), ("gc", "0")], timeout_s=120)
        await self._send("C0", "PR", [*self._xs(dx), ("yj", pad(dy * 10, 4)), ("yd", "0"), ("zj", pad(dz * 10, 4)), ("zd", "0"),
                                      *common, ("gr", "1"), ("go", pad((width + 3) * 10, 4)), ("ga", "0"), ("gc", "0")], timeout_s=120)
        await self._park_iswap()

    async def stop(self) -> None:
        # No abort is known for a STAR command in flight; the frontend stops sending commands.
        log.warning("Hamilton STAR: stop requested; the current firmware command finishes, no further commands are sent.")

    def info(self) -> dict[str, Any]:
        return {**super().info(), "model": self.deck.model, "channels": self.channels, "iswap": self.iswap_present,
                "core96": self.core96_present, **self.firmware.transport.describe()}
