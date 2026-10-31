"""Avay Bioscience LiquidXpert Lite: a 12-channel, 300 µL head on a Marlin (G-code) gantry, over USB serial.

Protocol and machine facts as measured on the instrument:

- Marlin 2.1 at 250000 baud. Opening the port resets the controller; it prints a start-up banner.
  Every command is answered ``ok`` (for a move: queued). ``M400`` waits for queued moves, ``M114``
  reports ``X:.. Y:.. Z:.. A:.. Count ...``. Errors are ``Error:...`` / ``!!`` / ``echo:Unknown command``.
- Axes in mm: X gantry (0 = home side, at the trash), Y gantry (of the head's last channel), Z the
  holder (0 = top, larger = lower), A the plunger (larger = more liquid; 6 = start, 0 = blow-out,
  17.05 mm per 300 µL).
- The 12 channels sit 9 mm apart along Y and move together; the 8 nearest the reference end carry
  tips and span one column (rows A-H). Labware therefore lies with its long side along X.
- Home: plunger off its stop, then ``G28`` A, Z, Y, X one at a time. Tips are ejected by pulsing
  the solenoid (``M42 P10``) twice. The instrument cannot sense tips or liquid.
- Marlin cannot abort a move in flight here: the instrument's power switch is the emergency stop.

Deck: slots "1", "2", "3" plus a fixed trash. Each placed labware needs a calibration for its slot
and labware: for plates and reservoirs ``{x, y, bottom_z, top_z}`` (head over column 1, Y of row A,
Z with the tip ends at the bottom and at the rim); for tip racks ``{x, y, pickup_z}``. FlowXpert's
stored positions are built in (``OFFICIAL``): its three 96-well plates on slots 1 and 2, the Tarsons
T524090 reservoir on slot 2, and the Twelve_300 tip position on slot 3. Give others with
``calibrations``. Only the horizontal orientation is used: in FlowXpert's vertical one all 12
channels take tips along a row, which pyreagent's 8-channel model does not cover.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

from ....core.errors import CommunicationError, DeviceTimeout, HardwareError, InvalidArgument, NeedsAttention
from ....io import SerialTransport
from ....io.transport import Transport
from ....labware import DEFAULT_CATALOG, Labware, parse_well
from ..backend import LiquidHandlerBackend
from ..models import PipetteInfo

log = logging.getLogger("pyreagent.avay")

PIPETTE = "head"
BAUDRATE = 250000
PITCH = 9.0
TIP_SPAN = 7 * PITCH
"""The head's Y is row A's Y minus this: the 8 tip channels are its last 8."""

QUERY_TIMEOUT_S = 5.0
MOTION_TIMEOUT_S = 120.0
BOOT_TIMEOUT_S = 6.0
BOOT_QUIET_S = 1.0
TOLERANCE_MM = 0.05

MAX_X, MAX_Y, MAX_Z = 525.0, 150.0, 195.0
TRAVEL_Z = 40.0
Z_SPEED = 2200 / 60
LIQUID_Z_SPEED = 10.0
RIM_CLEARANCE = 2.0
SETTLE_S = 1.0
MIN_HEIGHT_ABOVE = 3.0

PLUNGER_START = 6.0
PLUNGER_BLOW_OUT = 0.0
PLUNGER_MM_PER_UL = 17.05 / 300
PLUNGER_MAX = PLUNGER_START + 300 * PLUNGER_MM_PER_UL
PLUNGER_SPEED = 500 / 60
PLUNGER_ROUNDING = 0.002
DEFAULT_FLOW_UL_S = 150.0

TRASH = (0.0, 50.0, 110.0)
EJECT_PULSES = 2
EJECT_PULSE_S = 0.3

_POSITION = re.compile(r"([XYZA]):(-?\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class Calibration:
    """Machine coordinates of a slot's labware (see the module docstring)."""

    x: float
    y: float
    bottom_z: float | None = None
    top_z: float | None = None
    pickup_z: float | None = None
    pitch_x: float = PITCH
    """Distance between columns (troughs) along X."""


# FlowXpert's stored positions (Data\C1.json, C2.json, Pipette.json) for the 12-channel head with the
# labware horizontal (long side along X): its single-channel values shifted by X +1.5, Y +48.1, Z -24.2,
# as FlowXpert does for this head. ``top_z`` is FlowXpert's "touch" height near the rim; ``y`` is row A,
# so the head stands 7 pitches in front of it (the reservoir's stored head Y is 49.4).
OFFICIAL_LABWARE = [
    Labware(id="nunc_165305_96_wellplate_300ul", kind="plate", description="Nunc 165305 96-well plate", rows=8, columns=12,
            well_volume_ul=300),
    Labware(id="tarsons_941196_96_wellplate_300ul", kind="plate", description="Tarsons 941196 96-well plate", rows=8, columns=12,
            well_volume_ul=300),
    Labware(id="nunc_442404_96_wellplate_300ul", kind="plate", description="Nunc 442404 96-well plate", rows=8, columns=12,
            well_volume_ul=300),
    Labware(id="tarsons_t524090_2_reservoir_60ml", kind="reservoir", description="Tarsons T524090 reservoir, 2 troughs 47 x 120 mm",
            rows=1, columns=2, well_volume_ul=60000),
]
_PLATES_96 = ("nunc_165305_96_wellplate_300ul", "tarsons_941196_96_wellplate_300ul", "nunc_442404_96_wellplate_300ul")
OFFICIAL: dict[tuple[str, str], Calibration] = {
    **{("1", plate): Calibration(x=70.5, y=130.3, bottom_z=98.3, top_z=87.3) for plate in _PLATES_96},
    **{("2", plate): Calibration(x=238.6, y=130.4, bottom_z=98.1, top_z=87.1) for plate in _PLATES_96},
    ("2", "tarsons_t524090_2_reservoir_60ml"): Calibration(x=258.0, y=49.4 + TIP_SPAN, bottom_z=112.4, top_z=84.4, pitch_x=60.0),
}
TIP_RACK = Calibration(x=405.8, y=130.1, pickup_z=127.8)
"""FlowXpert's horizontal tip position for the Twelve_300 head (column 1, row A), on slot 3, for any 96-tip rack."""


def fmt(value: float) -> str:
    """G-code number without trailing zeros: 423.4, 40."""
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def parse_position(line: str) -> tuple[float, float, float, float] | None:
    """(x, y, z, plunger) of an ``M114`` report, else None."""
    axes = {axis: float(value) for axis, value in _POSITION.findall(line.split("Count", 1)[0])}
    if set(axes) != {"X", "Y", "Z", "A"}:
        return None
    return axes["X"], axes["Y"], axes["Z"], axes["A"]


class Marlin:
    """Line-based G-code: send a command, collect lines until its ``ok``; confirm every move."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self.position: tuple[float, float, float, float] | None = None
        self._lock = asyncio.Lock()

    async def _line(self, timeout_s: float) -> str:
        return (await self.transport.read_until(b"\n", timeout_s)).decode("ascii", "replace").strip()

    async def wait_for_boot(self) -> list[str]:
        """Collect the start-up banner until the output has been quiet for a second."""
        lines: list[str] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BOOT_TIMEOUT_S
        while loop.time() < deadline:
            try:
                line = await self._line(min(BOOT_QUIET_S, max(deadline - loop.time(), 0.01)))
            except TimeoutError:
                if lines:
                    break
                continue
            if line:
                lines.append(line)
        return lines

    async def send(self, command: str, timeout_s: float = QUERY_TIMEOUT_S) -> list[str]:
        async with self._lock:
            log.debug("liquidxpert >> %s", command)
            await self.transport.write(command.encode("ascii") + b"\n")
            lines: list[str] = []
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_s
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise DeviceTimeout(f"No 'ok' from the LiquidXpert for {command!r} within {timeout_s:g} s.",
                                        details={"command": command})
                try:
                    line = await self._line(remaining)
                except TimeoutError:
                    continue
                if not line:
                    continue
                log.debug("liquidxpert << %s", line)
                if line.startswith(("Error", "!!")) or "Unknown command" in line:
                    raise self._error(command, line)
                if line == "ok" or line.startswith("ok "):
                    return lines
                if not line.startswith("echo:busy"):
                    lines.append(line)

    @staticmethod
    def _error(command: str, line: str) -> Exception:
        details = {"command": command, "reply": line}
        if "kill()" in line:
            return NeedsAttention(f"The LiquidXpert controller halted on {command!r}: {line}",
                                  suggested_action="Check the instrument is switched on (the controller answers over USB even "
                                  "when the motors have no power), then reconnect: that resets the controller.", details=details)
        if "Unknown command" in line:
            return CommunicationError(f"The LiquidXpert does not know {command!r}: {line}", recoverable=False, details=details)
        return HardwareError(f"The LiquidXpert reported an error on {command!r}: {line}", details=details)

    async def request_position(self) -> tuple[float, float, float, float]:
        for line in await self.send("M114"):
            position = parse_position(line)
            if position is not None:
                return position
        raise CommunicationError("M114 did not report a position.")

    async def finish_moves(self) -> None:
        await self.send("M400", timeout_s=MOTION_TIMEOUT_S)

    async def move(self, speed: float, *, x: float | None = None, y: float | None = None, z: float | None = None,
                   plunger: float | None = None) -> None:
        """Move, wait until stopped, and check every axis ended where it should (moved or not)."""
        targets = {axis: None if v is None else round(v, 3) for axis, v in (("X", x), ("Y", y), ("Z", z), ("A", plunger))}
        words = [f"{axis}{fmt(v)}" for axis, v in targets.items() if v is not None]
        if not words:
            return
        await self.send(f"G0 {' '.join(words)} F{fmt(speed * 60)}")
        await self.finish_moves()
        reported = dict(zip("XYZA", await self.request_position()))
        expected = dict(targets)
        if self.position is not None:
            for axis, last in zip("XYZA", self.position):
                if expected[axis] is None:
                    expected[axis] = last
        for axis, target in expected.items():
            if target is not None and abs(reported[axis] - target) > TOLERANCE_MM:
                raise HardwareError(f"The LiquidXpert {axis} axis stopped at {reported[axis]:g} mm, expected {target:g} mm.",
                                    details={"axis": axis, "reported": reported[axis], "expected": target})
        # reports are rounded to 0.01 mm; keep what was sent so plunger volumes add up exactly
        self.position = tuple(reported[a] if expected[a] is None else expected[a] for a in "XYZA")  # type: ignore[assignment]

    async def dwell(self, seconds: float) -> None:
        await self.send(f"G4 P{round(seconds * 1000)}", timeout_s=QUERY_TIMEOUT_S + seconds)

    async def home(self) -> None:
        # Homing the plunger while it rests on its stop fails: move it off first, as the vendor software does.
        await self.send(f"G0 A{fmt(PLUNGER_START)} F{fmt(PLUNGER_SPEED * 60)}")
        await self.finish_moves()
        for axis in "AZYX":
            await self.send(f"G28 {axis}0", timeout_s=MOTION_TIMEOUT_S)
            await self.finish_moves()
        self.position = await self.request_position()
        x, y, _, plunger = self.position
        if (x, y, plunger) != (0, 0, 0):
            raise HardwareError(f"Homing ended at X{x:g} Y{y:g} A{plunger:g}; expected X, Y and the plunger at 0.")

    async def solenoid(self, on: bool) -> None:
        await self.send(f"M42 P10 S{255 if on else 0}")


class AvayLiquidXpertBackend(LiquidHandlerBackend):
    vendor = "Avay Bioscience"
    model = "LiquidXpert Lite"
    slots = ["1", "2", "3"]
    features = frozenset({"blow_out", "return_tips", "move_to"})

    def __init__(
        self,
        *,
        port: str = "",
        calibrations: Mapping[str, Mapping[str, Any]] | None = None,
        travel_speed_mm_s: float = 50.0,
        aspirate_height_mm: float = 4.0,
        dispense_height_mm: float = 5.0,
        tip_volumes_ul: list[float] | None = None,
        transport: Transport | None = None,
    ) -> None:
        """
        Args:
            port: serial port, e.g. "COM4" or "/dev/ttyUSB0".
            calibrations: per slot and labware id, adding to or overriding FlowXpert's:
                ``{"2": {"my_plate": {"x": .., "y": .., "bottom_z": .., "top_z": ..}}}`` (``pitch_x`` for troughs
                that are not 9 mm apart), or ``{"3": {"x": .., "y": .., "pickup_z": ..}}`` for whatever is on the slot.
            travel_speed_mm_s: gantry speed between positions; FlowXpert moves at 50 mm/s (3000 mm/min) to labware.
            aspirate_height_mm / dispense_height_mm: tip ends above the well bottom.
            tip_volumes_ul: tip sizes the head takes (default: 300 µL, the size checked on the instrument).
        """
        if not math.isfinite(travel_speed_mm_s) or travel_speed_mm_s <= 0:
            raise ValueError("travel_speed_mm_s must be greater than zero")
        self.marlin = Marlin(transport or SerialTransport(port, baudrate=BAUDRATE))
        for labware in OFFICIAL_LABWARE:
            DEFAULT_CATALOG.add(labware)
        self.calibrations: dict[tuple[str, str | None], Calibration] = {}
        for slot, values in (calibrations or {}).items():
            if slot not in self.slots:
                raise ValueError(f"calibrations for unknown slot {slot!r}; slots are 1, 2, 3")
            if "x" in values:
                self.calibrations[(slot, None)] = Calibration(**values)
            else:
                for labware_id, fields in values.items():
                    self.calibrations[(slot, labware_id)] = Calibration(**fields)
        self.travel_speed = travel_speed_mm_s
        self.aspirate_height_mm, self.dispense_height_mm = aspirate_height_mm, dispense_height_mm
        self.tip_volumes_ul = tip_volumes_ul or [300.0]
        self.firmware: str | None = None

    @property
    def pipettes(self) -> dict[str, PipetteInfo]:
        return {PIPETTE: PipetteInfo(name=PIPETTE, channels=8, min_volume_ul=10.0, max_volume_ul=300.0,
                                     default_flow_rate_ul_s=DEFAULT_FLOW_UL_S, tip_volumes_ul=self.tip_volumes_ul)}

    # -- deck ---------------------------------------------------------------------------------------

    def _calibration(self, slot: str, labware: Labware) -> Calibration:
        kind = "tip_rack" if labware.kind == "tip_rack" else ("reservoir" if labware.rows == 1 else "plate")
        calibration = (self.calibrations.get((slot, labware.id)) or self.calibrations.get((slot, None))
                       or OFFICIAL.get((slot, labware.id)) or (TIP_RACK if (slot, kind) == ("3", "tip_rack") else None))
        if calibration is None:
            raise InvalidArgument(
                f"No calibration for {labware.id} on slot {slot}.",
                suggested_action="Use labware FlowXpert has positions for (see details), or teach the position and add it to the "
                "driver's calibrations.",
                details={"calibrated": sorted(f"{lw} on {s}" for s, lw in OFFICIAL) + ["any 96-tip rack on 3"]})
        needed = ("pickup_z",) if kind == "tip_rack" else ("bottom_z", "top_z")
        if any(getattr(calibration, field) is None for field in needed):
            raise InvalidArgument(f"The calibration for slot {slot} has no {' / '.join(needed)}, which a {kind.replace('_', ' ')} needs.")
        return calibration

    def check_placement(self, slot: str, labware: Labware) -> None:
        if slot not in self.slots:
            raise InvalidArgument(f"No deck slot {slot!r}; the LiquidXpert has slots 1, 2 and 3.")
        if labware.kind in ("plate", "tip_rack") and (labware.rows, labware.columns) != (8, 12):
            raise InvalidArgument(f"The head addresses 96-position (8 x 12) layouts; {labware.id} is {labware.rows} x {labware.columns}.")
        if labware.kind == "reservoir" and labware.rows != 1:
            raise InvalidArgument(f"{labware.id}: a reservoir needs a single row of troughs.")
        if labware.kind not in ("plate", "tip_rack", "reservoir"):
            raise InvalidArgument(f"The LiquidXpert takes plates, reservoirs and tip racks, not a {labware.kind}.")
        self._calibration(slot, labware)

    def _over(self, slot: str, labware: Labware, well: str) -> tuple[float, float, Calibration]:
        """(x, y) of the head over the column of ``well``, and the slot's calibration."""
        calibration = self._calibration(slot, labware)
        _, column = parse_well(well)
        return calibration.x + (column - 1) * calibration.pitch_x, calibration.y - TIP_SPAN, calibration

    # -- connection ---------------------------------------------------------------------------------

    async def connect(self) -> None:
        await self.marlin.transport.open()
        try:
            await self.marlin.wait_for_boot()  # opening the port resets the controller
            for line in await self.marlin.send("M115"):
                match = re.search(r"FIRMWARE_NAME:(.+?) SOURCE_CODE_URL", line)
                if match:
                    self.firmware = match.group(1)
            if self.firmware is None:
                raise CommunicationError("The device on this port did not identify as Marlin firmware (M115).")
            await self.home()
        except BaseException:
            await self.marlin.transport.close()
            raise

    async def disconnect(self) -> None:
        await self.marlin.transport.close()

    # -- motion -------------------------------------------------------------------------------------

    async def home(self) -> None:
        """Home plunger, holder, then gantry, and raise the holder. Before each axis the holder dips 5 mm."""
        await self.marlin.home()
        await self.marlin.move(Z_SPEED, z=0)

    async def _travel_to(self, x: float, y: float) -> None:
        position = self.marlin.position
        if position is None or position[2] > TRAVEL_Z:
            await self.marlin.move(Z_SPEED, z=TRAVEL_Z)
        await self.marlin.move(self.travel_speed, x=x, y=y)

    def _plunger(self) -> float:
        if self.marlin.position is None:
            raise NeedsAttention("The plunger position is unknown; call home.")
        return self.marlin.position[3]

    async def _eject(self) -> None:
        for _ in range(EJECT_PULSES):
            for on in (True, False):
                await self.marlin.solenoid(on)
                await self.marlin.dwell(EJECT_PULSE_S)
                await self.marlin.finish_moves()

    async def pick_up_tips(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str) -> None:
        x, y, calibration = self._over(slot, labware, well)
        await self.marlin.move(Z_SPEED, z=TRAVEL_Z)
        await self.marlin.move(PLUNGER_SPEED, plunger=PLUNGER_START)
        await self._travel_to(x, y)
        assert calibration.pickup_z is not None
        await self.marlin.move(Z_SPEED, z=calibration.pickup_z)  # pressing onto the tips seats them
        await self.marlin.move(Z_SPEED, z=TRAVEL_Z)

    async def drop_tips(self, *, pipette: PipetteInfo) -> None:
        x, y, z = TRASH
        await self._travel_to(x, y)
        await self.marlin.move(Z_SPEED, z=z)
        await self._eject()
        await self.marlin.move(Z_SPEED, z=TRAVEL_Z)

    async def return_tips(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, drop_height_mm: float = 5.0) -> None:
        x, y, calibration = self._over(slot, labware, well)
        assert calibration.pickup_z is not None
        await self._travel_to(x, y)
        await self.marlin.move(LIQUID_Z_SPEED, z=calibration.pickup_z - drop_height_mm)
        await self._eject()
        await self.marlin.move(Z_SPEED, z=TRAVEL_Z)

    def _liquid_z(self, calibration: Calibration, height_mm: float) -> float:
        assert calibration.bottom_z is not None and calibration.top_z is not None
        depth = calibration.bottom_z - calibration.top_z
        if not 0 <= height_mm <= depth:
            raise InvalidArgument(f"The tip height {height_mm:g} mm is outside this labware's {depth:g} mm depth.")
        return calibration.bottom_z - height_mm

    async def _enter(self, x: float, y: float, calibration: Calibration, z: float) -> None:
        assert calibration.top_z is not None
        await self._travel_to(x, y)
        await self.marlin.move(Z_SPEED, z=calibration.top_z - RIM_CLEARANCE)
        await self.marlin.move(LIQUID_Z_SPEED, z=z)

    async def aspirate(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, volume_ul: float,
                       flow_rate_ul_s: float | None) -> None:
        plunger = self._plunger() + volume_ul * PLUNGER_MM_PER_UL
        if plunger > PLUNGER_MAX + PLUNGER_ROUNDING:
            raise InvalidArgument(f"Aspirating {volume_ul:g} µL would exceed the 300 µL plunger travel.")
        x, y, calibration = self._over(slot, labware, well)
        await self._enter(x, y, calibration, self._liquid_z(calibration, self.aspirate_height_mm))
        await self.marlin.move((flow_rate_ul_s or DEFAULT_FLOW_UL_S) * PLUNGER_MM_PER_UL, plunger=min(plunger, PLUNGER_MAX))
        await self.marlin.dwell(SETTLE_S)
        await self._leave(calibration)

    async def _leave(self, calibration: Calibration) -> None:
        assert calibration.top_z is not None
        await self.marlin.move(LIQUID_Z_SPEED, z=calibration.top_z - RIM_CLEARANCE)
        await self.marlin.move(Z_SPEED, z=TRAVEL_Z)

    async def dispense(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, volume_ul: float,
                       flow_rate_ul_s: float | None) -> None:
        plunger = self._plunger() - volume_ul * PLUNGER_MM_PER_UL
        if plunger < PLUNGER_START - PLUNGER_ROUNDING:
            raise NeedsAttention(f"The tips hold less than {volume_ul:g} µL by the plunger position.")
        x, y, calibration = self._over(slot, labware, well)
        await self._enter(x, y, calibration, self._liquid_z(calibration, self.dispense_height_mm))
        await self.marlin.move((flow_rate_ul_s or DEFAULT_FLOW_UL_S) * PLUNGER_MM_PER_UL, plunger=max(plunger, PLUNGER_START))
        await self.marlin.dwell(SETTLE_S)
        await self._leave(calibration)

    async def blow_out(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, flow_rate_ul_s: float | None) -> None:
        """Push the plunger past its start with the tips in the well, then reset it once they are out."""
        x, y, calibration = self._over(slot, labware, well)
        assert calibration.top_z is not None
        await self._enter(x, y, calibration, self._liquid_z(calibration, self.dispense_height_mm))
        await self.marlin.move(PLUNGER_SPEED, plunger=PLUNGER_BLOW_OUT)
        await self.marlin.dwell(SETTLE_S)
        await self.marlin.move(LIQUID_Z_SPEED, z=calibration.top_z - RIM_CLEARANCE)
        await self.marlin.move(PLUNGER_SPEED, plunger=PLUNGER_START)
        await self.marlin.move(Z_SPEED, z=TRAVEL_Z)

    async def move_to(self, *, pipette: PipetteInfo, slot: str, labware: Labware, well: str, height_mm: float) -> None:
        """Stop ``height_mm`` above the rim (tip racks: above the pickup height), to check a calibration by eye."""
        if height_mm < MIN_HEIGHT_ABOVE:
            raise InvalidArgument(f"Stay at least {MIN_HEIGHT_ABOVE:g} mm above the labware.")
        x, y, calibration = self._over(slot, labware, well)
        top = calibration.pickup_z if labware.kind == "tip_rack" else calibration.top_z
        assert top is not None
        await self._travel_to(x, y)
        await self.marlin.move(LIQUID_Z_SPEED, z=max(top - height_mm, 0.0))

    async def stop(self) -> None:
        # Marlin here cannot abort a move in flight; the power switch is the emergency stop.
        log.warning("Avay LiquidXpert: stop requested; the current move finishes. Use the power switch to stop at once.")

    def info(self) -> dict[str, Any]:
        return {**super().info(), "firmware": self.firmware, "limits_mm": {"x": MAX_X, "y": MAX_Y, "z": MAX_Z},
                **self.marlin.transport.describe()}
