"""Tecan Freedom EVO (LiHa 8-channel arm with DiTis, RoMa plate gripper) over its USB firmware protocol.

Protocol knowledge comes from PyLabRobot's reverse-engineered EVO backend (MIT); this is an
independent implementation in pyreagent's driver model. Command sequences reproduce the ones
PyLabRobot's tests expect (see ``tests/drivers/test_tecan_evo.py``).

Deck: carriers on rails, configured as ``carriers = {name = {type, rail}}``; slots are
"<carrier>.<site>". Labware needs physical x/y geometry plus EVOware z parameters: use the
``tecan_*`` labware (from ``vendors.tecan.worktable``) or give ``labware_params`` for your own.

Not supported: the MCA 96 head (initialised and parked only), reading back the detected liquid
level, aborting a firmware command in flight (``stop`` cannot interrupt the arm), 4-channel LiHas.
Constants PyLabRobot marks unverified (y origins, µL-to-step factors, the tip-pickup z offset)
are driver options.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Mapping

from ....core.errors import InvalidArgument, NeedsAttention, PyreagentError
from ....io import UsbBulkTransport
from ....labware import DEFAULT_CATALOG, Labware
from ....vendors.tecan.firmware import LIHA, MCA, NOT_PRESENT, PRODUCT_ID, ROMA, VENDOR_ID, EvoFirmware, PacketTransport
from ....vendors.tecan.liquid_classes import LiquidClass, water_diti
from ....vendors.tecan.worktable import TECAN_LABWARE, TecanParams, Worktable
from ..backend import LiquidHandlerBackend
from ..models import NozzleLayout, PipetteInfo
from ..nozzles import wells_touched
from ..positions import channel_targets

log = logging.getLogger("pyreagent.tecan")

PIPETTE = "liha"
AIR_BEFORE_PICKUP_STEPS = 30


class TecanEvoBackend(LiquidHandlerBackend):
    vendor = "Tecan"
    model = "Freedom EVO"

    def __init__(
        self,
        *,
        carriers: Mapping[str, Mapping[str, Any]],
        model: str = "evo150",
        serial_number: str | None = None,
        labware_params: Mapping[str, Mapping[str, float]] | None = None,
        waste_xy: tuple[int, int] = (45, 1031),
        z_travel_mm: float = 210.0,
        roma_z_travel_mm: float = 68.7,
        y_origin_mm: float = 346.5,
        roma_y_origin_mm: float = 347.1,
        steps_per_ul: float = 3.0,
        half_steps_per_ul_s: float = 6.0,
        tip_pickup_z_offset: int = 227,
        prime_on_connect: bool = True,
        timeout_s: float = 60.0,
        transport: PacketTransport | None = None,
    ) -> None:
        """
        Args:
            carriers: ``{"plates": {"type": "MP_3Pos", "rail": 16}, ...}``; slots become "plates.1".."plates.3".
            model: "evo100", "evo150" or "evo200" (number of rails).
            labware_params: EVOware z parameters for labware that has none here,
                ``{"my_plate": {"z_start": .., "z_dispense": .., "z_max": .., "area": ..}}``
                (tip racks: also ``tip_z_mm``).
            waste_xy: LiHa firmware x/y over the waste trough (default: the wash station on rail 1).
        """
        self.worktable = Worktable(model, {name: (str(c["type"]), int(c["rail"])) for name, c in carriers.items()})
        self.slots = self.worktable.slots
        for labware, _ in TECAN_LABWARE.values():
            DEFAULT_CATALOG.add(labware)
        self.params: dict[str, TecanParams] = {k: p for k, (_, p) in TECAN_LABWARE.items()}
        self.params.update({k: TecanParams(**v) for k, v in (labware_params or {}).items()})
        self.firmware = EvoFirmware(transport or UsbBulkTransport(VENDOR_ID, PRODUCT_ID, serial_number=serial_number, read_size=4096),
                                    timeout_s=timeout_s)
        self.waste_xy = waste_xy
        self.z_travel = round(z_travel_mm * 10)
        self.roma_z_travel = round(roma_z_travel_mm * 10)
        self.y_origin_mm, self.roma_y_origin_mm = y_origin_mm, roma_y_origin_mm
        self.steps_per_ul, self.half_steps_per_ul_s = steps_per_ul, half_steps_per_ul_s
        self.tip_pickup_z_offset = tip_pickup_z_offset
        self.prime_on_connect = prime_on_connect
        self.channels = 8
        self.z_range = 2000
        self.roma_z_range = 2000
        self.roma_present = self.mca_present = False
        self._layout = NozzleLayout()
        self._tip_z: dict[int, float] = {}
        self._steps: dict[int, float] = {}
        self._liquid: dict[int, float] = {}
        self._class: dict[int, LiquidClass] = {}
        self._liha_x: int | None = None
        self._roma_x: int | None = None

    # -- facts --------------------------------------------------------------------------------

    @property
    def pipettes(self) -> dict[str, PipetteInfo]:
        return {PIPETTE: PipetteInfo(name=PIPETTE, channels=8, min_volume_ul=3.0, max_volume_ul=1000.0,
                                     tip_volumes_ul=[10.0, 50.0, 100.0, 200.0, 1000.0], independent_channels=True)}

    @property
    def features(self) -> frozenset[str]:  # type: ignore[override]
        features = {"return_tips", "move_to", "nozzle_layouts", "move_labware"}
        if self.roma_present:
            features.add("gripper")
        return frozenset(features)

    def _params(self, labware: Labware) -> TecanParams:
        try:
            return self.params[labware.id]
        except KeyError:
            raise InvalidArgument(
                f"{labware.id} has no EVOware z parameters.",
                suggested_action="Use tecan_* labware from GET /labware, or add z_start/z_dispense/z_max/area for it in "
                "the driver's labware_params.",
            ) from None

    def check_placement(self, slot: str, labware: Labware) -> None:
        labware.require_geometry()
        params = self._params(labware)
        if labware.kind == "tip_rack" and params.tip_z_mm is None:
            raise InvalidArgument(f"{labware.id} has no tip_z_mm in its Tecan parameters.")
        self.worktable.site(slot)

    # -- connection ----------------------------------------------------------------------------

    async def _send(self, module: str, command: str, params: list[int | None] | tuple[int | None, ...] = ()) -> list[Any]:
        return await self.firmware.send(module, command, params)

    async def _present(self, module: str, command: str) -> bool:
        try:
            await self._send(module, command)
            return True
        except PyreagentError as err:
            if err.details.get("code") == NOT_PRESENT:
                return False
            raise

    async def connect(self) -> None:
        await self.firmware.transport.open()
        try:
            await self._initialise()
        except BaseException:
            await self.firmware.transport.close()
            raise

    async def _initialise(self) -> None:
        self.firmware.forget()
        if not await self._present(LIHA, "PIA"):
            raise NeedsAttention("This EVO reports no LiHa pipetting arm.")
        await self._send(LIHA, "BMX", [2])
        self.mca_present = await self._present(MCA, "PIB") and await self._present(MCA, "PIA")
        self.roma_present = await self._present(ROMA, "PIA")
        if self.roma_present:
            await self._send(ROMA, "BMX", [2])
            await self._send(ROMA, "PIX")
            await self._send(ROMA, "SAA", [1, 9000, 2000, 2464, 1800, None, 1, 0, 0])
            await self._send(ROMA, "AAC")
            self._roma_x = 9000
        if self.mca_present:
            for command, params in (("PIA", []), ("PAA", [None, None, 2000]), ("PAA", [6000, 1000, None]), ("BMA", [0, 0, 0])):
                await self._send(MCA, command, params)
        await self._send(LIHA, "PIX")
        self.channels = int((await self._send(LIHA, "RNT", [1]))[0])
        if self.channels != 8:
            raise NeedsAttention(f"This LiHa has {self.channels} channels; the pyreagent EVO driver supports 8.")
        self.z_range = int((await self._send(LIHA, "RPZ", [5]))[0])
        if self.roma_present:
            self.roma_z_range = int((await self._send(ROMA, "RPZ", [5]))[0])
        self._tip_z, self._steps, self._liquid, self._class = {}, {}, {}, {}
        if self.prime_on_connect:
            await self._prime()

    async def _prime(self) -> None:
        """Flush the plungers over the waste, then park (as EVOware does at start-up)."""
        n, zr = self.channels, self.z_range
        wx, wy = self.waste_xy
        await self._send(LIHA, "SHZ", [zr] * n)
        await self._move_liha(wx, wy, 90, [1200] * n)
        await self._send(LIHA, "PID", [(1 << n) - 1])
        await self._send(LIHA, "PVL", [1] * n)
        await self._send(LIHA, "PPR", [100] * n)
        await self._send(LIHA, "PVL", [0] * n)
        await self._send(LIHA, "SEP", [1800] * n)
        await self._send(LIHA, "PPR", [-100] * n)
        await self._move_liha(wx, wy, 90, [zr] * n)

    async def disconnect(self) -> None:
        await self.firmware.transport.close()

    # -- coordinates -----------------------------------------------------------------------

    def _per(self, values: Mapping[int, int | None]) -> list[int | None]:
        return [values.get(ch) for ch in range(self.channels)]

    def _targets(self, slot: str, labware: Labware, well: str, layout: NozzleLayout) -> tuple[int, int, int, list[int]]:
        """(x, y of channel 0, y spacing, channels used) for the wells the active channels touch."""
        site = self.worktable.site(slot)
        targets = channel_targets(site, labware, wells_touched(labware, well, 8, layout))
        xs = {round((t.x_mm - 100) * 10) for t in targets}
        if len(xs) != 1:
            raise InvalidArgument("The LiHa channels move together in x; all wells must be in one column.")
        ys = [round((self.y_origin_mm - t.y_mm) * 10) for t in targets]  # firmware y grows toward the front
        spacing = 90 if len(ys) == 1 else round((ys[-1] - ys[0]) / (len(ys) - 1))
        if any(abs(y - (ys[0] + i * spacing)) > 1 for i, y in enumerate(ys)):
            raise InvalidArgument("The LiHa needs evenly spaced channels; these wells are not.")
        if not 90 <= spacing <= 380:
            raise InvalidArgument(f"Channel spacing {spacing / 10:g} mm is outside the LiHa's 9-38 mm range.")
        return xs.pop(), ys[0], spacing, list(range(len(targets)))

    def _z(self, slot: str, labware: Labware, which: Literal["start", "dispense", "max"], tip_z_mm: float) -> int:
        params = self._params(labware)
        value = {"start": params.z_start, "dispense": params.z_dispense, "max": params.z_max}[which]
        return round(self.z_range - value + self.worktable.site(slot).z_mm * 10 + tip_z_mm * 10)

    async def _check_collision(self, arm: str, target_x: int) -> None:
        """Refuse a move that would cross or come within 150 mm of the other arm (as PyLabRobot does)."""
        current = (await self._send(arm, "RPX", [0]) or [None])[0]
        other = self._roma_x if arm == LIHA else self._liha_x
        if other is None or not isinstance(current, int):
            return
        if min(current, target_x) <= other <= max(current, target_x) or abs(other - target_x) < 1500:
            raise InvalidArgument(f"The {'RoMa' if arm == LIHA else 'LiHa'} is in the way of this move.",
                                  suggested_action="Move the other arm away (e.g. home it) and retry.")

    async def _move_liha(self, x: int, y: int, spacing: int, zs: list[int]) -> None:
        await self._check_collision(LIHA, x)
        await self._send(LIHA, "PAA", [x, y, spacing, *zs])
        self._liha_x = x

    # -- tips ------------------------------------------------------------------------------------

    async def home(self) -> None:
        await self._send(LIHA, "PIA")
        await self._send(LIHA, "PIX")
        self.firmware.forget()
        wx, wy = self.waste_xy
        await self._send(LIHA, "SHZ", [self.z_range] * self.channels)
        await self._move_liha(wx, wy, 90, [self.z_range] * self.channels)

    async def configure_nozzles(self, *, pipette: PipetteInfo, layout: NozzleLayout) -> None:
        self._layout = layout  # channels are independent: a layout only chooses how many are used

    async def pick_up_tips(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str) -> None:
        x, y, spacing, used = self._targets(slot, labware, well, self._layout)
        tip_z = self._params(labware).tip_z_mm or 0.0
        zr, n = self.z_range, self.channels
        await self._send(LIHA, "SHZ", [zr] * n)
        await self._move_liha(x, y, spacing, [zr] * n)
        await self._send(LIHA, "PVL", self._per({ch: 0 for ch in used}))
        await self._send(LIHA, "SEP", self._per({ch: 420 for ch in used}))
        await self._send(LIHA, "PPR", self._per({ch: AIR_BEFORE_PICKUP_STEPS for ch in used}))
        z_start = self._z(slot, labware, "start", tip_z)
        await self._send(LIHA, "AGT", [sum(1 << ch for ch in used), z_start - self.tip_pickup_z_offset, 210, 0])
        for ch in used:
            self._tip_z[ch], self._steps[ch], self._liquid[ch] = tip_z, 0.0, 0.0

    async def _release_tips(self, x: int, y: int, spacing: int) -> None:
        used = sorted(self._tip_z)
        zr, n = self.z_range, self.channels
        await self._send(LIHA, "SHZ", [zr] * n)
        await self._move_liha(x, y, spacing, [zr] * n)
        await self._send(LIHA, "AST", [sum(1 << ch for ch in used), 0])
        self._tip_z, self._steps, self._liquid, self._class = {}, {}, {}, {}

    async def drop_tips(self, *, pipette: PipetteInfo) -> None:
        await self._release_tips(*self.waste_xy, 90)

    async def return_tips(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str) -> None:
        x, y, spacing, _ = self._targets(slot, labware, well, self._layout)
        await self._release_tips(x, y, spacing)

    # -- liquid ----------------------------------------------------------------------------------

    def _steps_for(self, volume_ul: float) -> int:
        return round(volume_ul * self.steps_per_ul)

    def _speed(self, ul_per_s: float) -> int:
        return round(ul_per_s * self.half_steps_per_ul_s)

    async def aspirate(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, volume_ul: float,
                       flow_rate_ul_s: float | None) -> None:
        x, y, spacing, used = self._targets(slot, labware, well, self._layout)
        lc = water_diti(volume_ul)
        flow = flow_rate_ul_s or lc.aspirate_speed
        params = self._params(labware)
        zadd = round(volume_ul / params.area * 10) if params.area else 0
        zr, n, travel = self.z_range, self.channels, self.z_travel
        z_start = {ch: self._z(slot, labware, "start", self._tip_z.get(ch, 0.0)) for ch in used}
        z_max = {ch: self._z(slot, labware, "max", self._tip_z.get(ch, 0.0)) for ch in used}
        mask = sum(1 << ch for ch in used)

        await self._send(LIHA, "SHZ", [zr] * n)
        await self._move_liha(x, y, spacing, [travel if ch in used else zr for ch in range(n)])
        await self._send(LIHA, "PVL", self._per({ch: 0 for ch in used}))
        await self._send(LIHA, "SEP", self._per({ch: self._speed(lc.lag_speed) for ch in used}))
        await self._send(LIHA, "PPR", self._per({ch: self._steps_for(lc.lag_volume) for ch in used}))
        if lc.aspirate_lld:
            await self._send(LIHA, "SDM", [lc.lld_mode, lc.lld_conductivity])
            await self._send(LIHA, "SSL", self._per({ch: round(lc.lld_speed_mm_s * 10) for ch in used}))
            await self._send(LIHA, "SDL", self._per({ch: round(lc.lld_distance_mm * 10) for ch in used}))
            await self._send(LIHA, "STL", self._per(z_start))
            await self._send(LIHA, "SML", [z_max.get(ch, zr) for ch in range(n)])
            await self._send(LIHA, "SBL", self._per({ch: round(lc.lld_offset_mm * 10) for ch in used}))
            await self._send(LIHA, "SHZ", [travel] * n)
            await self._send(LIHA, "MDT", [mask, None, None, None, *[zadd if ch in used else 0 for ch in range(n)]])
            await self._send(LIHA, "SHZ", [zr] * n)
        zadd = min(zadd, 32)
        await self._send(LIHA, "SSZ", self._per({ch: round(zadd * flow / volume_ul) for ch in used}))
        await self._send(LIHA, "SEP", self._per({ch: self._speed(flow) for ch in used}))
        await self._send(LIHA, "STZ", self._per({ch: -zadd for ch in used}))
        await self._send(LIHA, "MTR", self._per({ch: self._steps_for(lc.corrected(volume_ul)) for ch in used}))
        await self._send(LIHA, "SSZ", self._per({ch: round(lc.aspirate_retract_speed_mm_s * 10) for ch in used}))
        await self._send(LIHA, "MAZ", self._per(z_start))
        await self._send(LIHA, "PVL", self._per({ch: 0 for ch in used}))
        await self._send(LIHA, "SEP", self._per({ch: self._speed(lc.tag_speed) for ch in used}))
        await self._send(LIHA, "PPR", self._per({ch: self._steps_for(lc.tag_volume) for ch in used}))
        for ch in used:
            self._steps[ch] = self._steps.get(ch, 0.0) + self._steps_for(lc.lag_volume) + self._steps_for(lc.corrected(volume_ul)) \
                + self._steps_for(lc.tag_volume)
            self._liquid[ch] = self._liquid.get(ch, 0.0) + volume_ul
            self._class[ch] = lc

    async def dispense(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, volume_ul: float,
                       flow_rate_ul_s: float | None) -> None:
        x, y, spacing, used = self._targets(slot, labware, well, self._layout)
        lc = self._class.get(used[0]) or water_diti(volume_ul)
        flow = flow_rate_ul_s or lc.dispense_speed
        zr, n = self.z_range, self.channels
        z_dispense = {ch: self._z(slot, labware, "dispense", self._tip_z.get(ch, 0.0)) for ch in used}
        await self._send(LIHA, "SHZ", [self.z_travel] * n)
        await self._move_liha(x, y, spacing, [z_dispense.get(ch, zr) for ch in range(n)])
        await self._send(LIHA, "SEP", self._per({ch: self._speed(flow) for ch in used}))
        await self._send(LIHA, "SPP", self._per({ch: self._speed(lc.dispense_breakoff) for ch in used}))
        await self._send(LIHA, "STZ", self._per({ch: 0 for ch in used}))
        steps: dict[int, int] = {}
        for ch in used:
            if volume_ul >= self._liquid.get(ch, 0.0) - 1e-6:
                steps[ch] = round(self._steps.get(ch, 0.0))  # empty the tip: liquid and air gaps
                self._steps[ch], self._liquid[ch] = 0.0, 0.0
            else:
                steps[ch] = self._steps_for(lc.calibration_factor * volume_ul)
                self._steps[ch] -= steps[ch]
                self._liquid[ch] -= volume_ul
        await self._send(LIHA, "MTR", self._per({ch: -s for ch, s in steps.items()}))

    async def move_to(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, height_mm: float) -> None:
        x, y, spacing, used = self._targets(slot, labware, well, self._layout)
        zs = {ch: min(self._z(slot, labware, "start", self._tip_z.get(ch, 0.0)) + round(height_mm * 10), self.z_travel) for ch in used}
        await self._send(LIHA, "SHZ", [self.z_range] * self.channels)
        await self._move_liha(x, y, spacing, [zs.get(ch, self.z_range) for ch in range(self.channels)])

    # -- RoMa ------------------------------------------------------------------------------------

    def _roma_position(self, slot: str, labware: Labware) -> tuple[int, int, dict[str, int]]:
        roma_x, roma_y, roma_z_safe, roma_z_end = self.worktable.roma(slot)
        site = self.worktable.site(slot)
        size_y = labware.require_geometry().size_y_mm
        x = round((site.x_mm - 100) * 10 + roma_x)
        y = round((self.roma_y_origin_mm - (site.y_mm + size_y)) * 10 + roma_y)
        zr = self.roma_z_range
        return x, y, {"safe": zr - round(roma_z_safe), "travel": self.roma_z_travel, "end": zr - round(roma_z_end - site.z_mm * 10)}

    async def _vector(self, index: int, x: int, y: int, z: int, target_window: int) -> None:
        await self._check_collision(ROMA, x)
        await self._send(ROMA, "SAA", [index, x, y, z, 900, None, 1, 0, target_window])
        self._roma_x = x

    async def move_labware(self, *, labware: Labware, from_slot: str, to_slot: str, use_gripper: bool) -> None:
        if not use_gripper:
            return  # a human moved it; the EVO keeps no labware state of its own
        sx, sy, sz = self._roma_position(from_slot, labware)
        dx, dy, dz = self._roma_position(to_slot, labware)
        # pick
        self.roma_z_range = int((await self._send(ROMA, "RPZ", [5]))[0])
        await self._send(ROMA, "SSM", [1])
        await self._send(ROMA, "SFX", [10000, None])
        await self._send(ROMA, "SFY", [5000, 1500])
        await self._send(ROMA, "SFZ", [1300, None])
        await self._send(ROMA, "SFR", [5000, 1500])
        await self._vector(1, sx, sy, sz["safe"], 0)
        await self._send(ROMA, "AAC")
        await self._send(ROMA, "SSM", [0])
        await self._send(ROMA, "PAG", [900])
        await self._send(ROMA, "STW", [1, 0, 0, 0, 135, 0])
        await self._vector(1, sx, sy, sz["travel"], 1)
        await self._vector(1, sx, sy, sz["end"], 0)
        await self._send(ROMA, "AAC")
        await self._send(ROMA, "SFY", [3500, 1000])
        await self._send(ROMA, "SFR", [2000, 600])
        await self._send(ROMA, "SGG", [100, 75, None])
        await self._send(ROMA, "AGR", [round(labware.require_geometry().size_y_mm * 10 - 100)])
        # place
        await self._send(ROMA, "RPZ", [5])
        for window in ([1, 0, 0, 0, 135, 0], [2, 0, 0, 0, 53, 0], [3, 0, 0, 0, 55, 0], [4, 45, 0, 0, 0, 0]):
            await self._send(ROMA, "STW", window)
        for index, (x, y, z, tw) in enumerate([(sx, sy, sz["end"], 1), (sx, sy, sz["travel"], 2), (sx, sy, sz["safe"], 3),
                                                (dx, dy, dz["safe"], 4), (dx, dy, dz["travel"], 3), (dx, dy, dz["end"], 0)], start=1):
            await self._vector(index, x, y, z, tw)
        await self._send(ROMA, "AAC")
        await self._send(ROMA, "PAG", [900])
        await self._send(ROMA, "SFY", [5000, 1500])
        await self._send(ROMA, "SFR", [5000, 1500])
        for index, (z, tw) in enumerate([(dz["end"], 1), (dz["travel"], 2), (dz["safe"], 0)], start=1):
            await self._vector(index, dx, dy, z, tw)
        await self._send(ROMA, "AAC")
        await self._send(ROMA, "SFY", [3500, 1000])
        await self._send(ROMA, "SFR", [2000, 600])

    async def stop(self) -> None:
        # The EVO firmware has no known abort for an arm move in flight; the frontend stops sending commands.
        log.warning("Tecan EVO: stop requested; the current firmware command finishes, no further commands are sent.")

    def info(self) -> dict[str, Any]:
        return {**super().info(), "model": self.worktable.model, "channels": self.channels, "roma": self.roma_present,
                "mca": self.mca_present, "z_range": self.z_range, **self.firmware.transport.describe()}
