"""BMG Labtech CLARIOstar and CLARIOstar Plus multimode readers over their FTDI serial link.

Protocol knowledge comes from studying PyLabRobot's BMG Labtech CLARIOstar backends and the
frames they recorded (MIT); this is an independent implementation in pyreagent's driver model.
Every recording was made on one CLARIOstar Plus with firmware 1.35, so other firmware gets a
warning, and the non-Plus CLARIOstar and the 2.x native-USB readers are untested.

Link: the reader's FTDI chip (VID 0x0403, PID 0xBB68) at 125000 baud, 8N1, no flow control.
The default transport is a COM port (``port="COM5"``). The stock FTDI VCP driver probably does not
bind to this PID, so it likely needs a custom INF (untested). The VCP driver keeps reading into a kernel
buffer, which matters: the reader's FT232H drops bytes when no USB read is pending. A raw libusb
transport would need an always-pending bulk read to be safe. Lowering the port's latency timer
to 2 ms (Device Manager > Advanced) shortens each exchange. Frames are read by their length
field, never up to a CR, because 0x0D occurs inside payloads.

One command is outstanding at a time and the next frame is its reply. Unread input is discarded
before each write, failed replies are retried (STATUS 5 times, other queries 3 times; INIT, the
drawer, RUN and data pages once, because resending them repeats or loses the action), and after a
failed or cancelled exchange the line is drained until it is quiet.

Supported:
- Absorbance at one wavelength, and spectra 220-1000 nm (any whole-nm step), as OD with the
  reader's calibration values applied (the formula reportedly matches the vendor's OD to < 0.0006).
- Fluorescence intensity through the monochromators, top or bottom optic, one channel per read:
  gain 0-4095 (default ``default_gain``), focal height 0-25 mm, 1-200 flashes. The band widths
  are driver options; the dichroic split is the midpoint of the facing band edges. Filters,
  multiple channels, EDR, fluorescence spectra and well scans (orbital, spiral, matrix) are not
  offered.
- Luminescence: top optic, point read, EDR on. ``integration_s`` is the reader's measurement
  interval per well; counts are normalized to ``luminescence_normalization_s``.
- Drawer, plate and temperature status; incubation 25-45 °C, or 10-65 °C with the extended
  incubator (``extended_temperature=True``). Heating only: the reader cannot cool, so a set
  point below the chamber temperature is reached only by drifting to ambient.
- Plates up to 384 wells; well positions are sent with the RUN, so plates need geometry.

Not verified on hardware (implemented from the protocol as recorded, flagged here):
- Luminescence was only ever run on an empty plate; RLU scaling is unconfirmed.
- Temperature set points: no accepted set-point frame was recorded. Off, monitor-only and an
  out-of-range refusal were.
- Fluorescence through the bottom optic was only recorded as a RUN frame, not a full read.

Shaking exists only as a shake-before-read step inside a RUN; it is not offered as a standalone
``shake`` and is a possible future read option. Kinetic reads, z-focus, auto-gain, filter
detection, injectors and the stacker are not implemented. The driver never sends command family
0x0E, which writes the reader's stored settings; the protocol module refuses to encode it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from ....core.errors import (
    CommunicationError, DeviceBusy, DeviceTimeout, HardwareError, InvalidArgument, NeedsAttention, PyreagentError,
)
from ....io import SerialTransport, Transport
from ....labware import Labware, parse_well, row_label
from ..backend import FluorescenceSettings, PlateReaderBackend, Progress
from ..models import AbsorbanceSpec, FluorescenceSpec, LuminescenceSpec, ReaderSpec
from .protocol import (
    DRAWER, INITIALIZE, INIT_REQUEST, LAYOUT_ABSORBANCE, LAYOUT_FLUORESCENCE, LAYOUT_LUMINESCENCE, MAX_WELLS,
    MODE_ABSORBANCE, MODE_FLUORESCENCE, MODE_LUMINESCENCE, QUIT_CODES, REPLY_FIRMWARE, REPLY_INIT, REPLY_PARAMETERS,
    REPLY_RUN_ACK, REPLY_STATUS, REQUEST, REQUEST_DATA, REQUEST_FIRMWARE, REQUEST_PARAMETERS, RUN, SCAN_DEFAULT,
    SCAN_UNIDIRECTIONAL, STATUS, STATUS_REQUEST, STOP, STOP_REQUEST, STX, TEMPERATURE, TEMPERATURE_MONITOR, TEMPERATURE_OFF,
    DataPage, FrameError, Hardware, PlateGeometry, Status, absorbance_run, absorbance_values_expected, band, channel_block,
    decode, decode_data, decode_firmware_info, decode_init, decode_parameters, dichroic_split, drawer, encode,
    fluorescence_run, frame_size, luminescence_run, optical_densities, run_start, setpoint, temperature,
)

log = logging.getLogger("pyreagent.clariostar")

BAUDRATE = 125_000
LINE_BYTES_PER_S = BAUDRATE / 10
CHUNK = 4096
FRAME_CAP_S = 60.0
DRAIN_CAP_S = 60.0
RETRY_SETTLE_S = 0.08
DRAWER_TIMEOUT_S = 20.0
IDLE_TIMEOUT_S = 60.0
CONFIRMED_FIRMWARE = {1350}
FOCAL_HEIGHT_MM = (0.0, 25.0)
ACTIONS = {INITIALIZE: "initialization", DRAWER: "the drawer command", RUN: "the measurement", REQUEST: "the data request",
           TEMPERATURE: "the temperature command", STOP: "stop", STATUS: "the status request"}


class ClariostarBackend(PlateReaderBackend):
    vendor = "BMG Labtech"

    def __init__(
        self,
        port: str | None = None,
        *,
        extended_temperature: bool = False,
        poll_interval_s: float = 0.25,
        read_timeout_s: float = 600.0,
        init_timeout_s: float = 150.0,
        reply_timeout_s: float = 1.0,
        silence_s: float = 2.0,
        settling_s: float = 0.1,
        unidirectional: bool = False,
        absorbance_flashes: int = 5,
        fluorescence_flashes: int = 10,
        default_gain: int = 1000,
        default_focal_height_mm: float = 8.5,
        excitation_bandwidth_nm: float = 15.0,
        emission_bandwidth_nm: float = 20.0,
        luminescence_emission_nm: float = 580.0,
        luminescence_bandwidth_nm: float = 80.0,
        luminescence_gain: int = 3500,
        luminescence_normalization_s: float = 1.0,
        require_plate: bool = True,
        max_plate_height_mm: float = 20.0,
        transport: Transport | None = None,
    ) -> None:
        """
        Args:
            port: COM port of the reader's FTDI VCP, e.g. ``"COM5"`` or ``"/dev/ttyUSB0"``.
            extended_temperature: The reader has the extended incubator (10-65 °C instead of 25-45 °C).
            poll_interval_s: Status poll interval while waiting (drawer, measurement, init).
            read_timeout_s: Base timeout of a measurement; twice the read's estimated duration is added.
            init_timeout_s: How long homing on connect may take (about 13 s normally).
            reply_timeout_s: Time for a reply to start.
            silence_s: A reply is abandoned, and draining ends, after this long without bytes.
            settling_s: Pause after each move before absorbance and fluorescence reads (0-1 s).
            unidirectional: Read every row in the same direction (default meanders).
            absorbance_flashes: Flashes per well for absorbance.
            fluorescence_flashes: Flashes per well when a fluorescence read does not give ``flashes``.
            default_gain: Fluorescence gain when a read does not give one (the reader has no automatic gain here).
            default_focal_height_mm: Fluorescence and luminescence read height when a read does not give one.
            excitation_bandwidth_nm / emission_bandwidth_nm: Fluorescence monochromator band widths (8-100 nm).
                The emission must clear the excitation by half their sum, which sets ``min_stokes_shift_nm``.
            luminescence_emission_nm / luminescence_bandwidth_nm: Luminescence emission band.
            luminescence_gain: Luminescence detector gain (0-4095).
            luminescence_normalization_s: Luminescence counts are normalized to this time.
            require_plate: Refuse to read unless the reader's plate sensor sees a plate. Turn off if
                your plates are not detected.
            max_plate_height_mm: Refuse to read plates taller than this; a cautious clearance guess.
            transport: Inject a transport (tests use a fake).
        """
        if port is None and transport is None:
            raise ValueError("port is required")
        if not 8 <= excitation_bandwidth_nm <= 100 or not 8 <= emission_bandwidth_nm <= 100:
            raise ValueError("monochromator band widths must be 8-100 nm")
        self.transport = transport or SerialTransport(port or "", baudrate=BAUDRATE)
        self.extended_temperature = extended_temperature
        self.poll_interval_s = poll_interval_s
        self.read_timeout_s = read_timeout_s
        self.init_timeout_s = init_timeout_s
        self.reply_timeout_s = reply_timeout_s
        self.silence_s = silence_s
        self.settling_s = settling_s
        self.scan = SCAN_DEFAULT | (SCAN_UNIDIRECTIONAL if unidirectional else 0)
        self.absorbance_flashes = absorbance_flashes
        self.fluorescence_flashes = fluorescence_flashes
        self.default_gain = default_gain
        self.default_focal_height_mm = default_focal_height_mm
        self.excitation_bandwidth_nm = excitation_bandwidth_nm
        self.emission_bandwidth_nm = emission_bandwidth_nm
        self.luminescence_band = band(luminescence_emission_nm, luminescence_bandwidth_nm)
        self.luminescence_gain = luminescence_gain
        self.luminescence_normalization_s = luminescence_normalization_s
        self.require_plate = require_plate
        self.max_plate_height_mm = max_plate_height_mm
        self.hardware = Hardware()
        self.identity: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._dirty = False  # an exchange was cut short: drain before the next one
        self._running = False  # a RUN is in progress
        self._target: float | None = None
        self._sensors_on = False
        self._warned: tuple[int, ...] = ()

    # -- what this reader can do ------------------------------------------------------------------

    @property
    def model(self) -> str:  # type: ignore[override]
        return "CLARIOstar Plus" if self.hardware.plus else "CLARIOstar"

    @property
    def _max_fluorescence_nm(self) -> int:
        return 840 if self.hardware.extended_red and not self.hardware.crossover else 740

    @property
    def spec(self) -> ReaderSpec:  # type: ignore[override]
        hw, params = self.hardware, self.hardware.parameters
        absorbance = fluorescence = luminescence = None
        if hw.spectrometer and params.get(133, 1):
            absorbance = AbsorbanceSpec(min_nm=220, max_nm=1000, spectrum=True, max_spectrum_points=781)
        if hw.monochromators and params.get(132, 1):
            top = self._max_fluorescence_nm
            fluorescence = FluorescenceSpec(
                min_excitation_nm=320, max_excitation_nm=top, min_emission_nm=320, max_emission_nm=top,
                min_stokes_shift_nm=int((self.excitation_bandwidth_nm + self.emission_bandwidth_nm) // 2) + 1,
                min_gain=0, max_gain=4095, optics=["top", "bottom"] if params.get(141, 1) else ["top"],
                focal_height_mm=FOCAL_HEIGHT_MM, max_flashes=200)
        if params.get(128, 1):
            luminescence = LuminescenceSpec(min_integration_s=0.1, max_integration_s=20.0, focal_height_mm=FOCAL_HEIGHT_MM)
        temperature_c = ((10.0, 65.0) if self.extended_temperature else (25.0, 45.0)) if hw.incubator else None
        return ReaderSpec(absorbance=absorbance, fluorescence=fluorescence, luminescence=luminescence, temperature_c=temperature_c,
                          tray=True, plate_formats=[(2, 3), (3, 4), (4, 6), (6, 8), (8, 12), (16, 24)], needs_geometry=True)

    # -- connection ---------------------------------------------------------------------------------

    async def connect(self) -> None:
        await self.transport.open()
        try:
            await self.transport.discard_input()
            self.hardware, self.identity, self._target, self._sensors_on = Hardware(), {}, None, False
            await self._initialize()
            status = await self._wait(lambda s: not s.busy, self.init_timeout_s, "initializing")
            if not status.motors_enabled:
                raise HardwareError("The CLARIOstar's motors are not enabled after initializing.",
                                    suggested_action="Ask a human to power-cycle the reader, then reset the device.")
            await self._discover()
            if self.hardware.incubator:
                try:
                    await self._command(temperature(TEMPERATURE_MONITOR))
                    self._sensors_on = True
                except PyreagentError as exc:
                    log.warning("could not turn the temperature sensors on: %s", exc.message)
        except BaseException:
            await self.transport.close()
            raise

    async def _initialize(self) -> None:
        """Send INIT once, without polling first (an uninitialized reader refuses STATUS)."""
        try:
            reply = await self._exchange(INIT_REQUEST, attempts=1)  # a resend would restart homing
        except (CommunicationError, DeviceTimeout) as exc:
            log.warning("no usable INIT reply (%s); waiting on status instead", exc.message)
            return
        if reply[0] == REPLY_STATUS:
            self._check(Status.decode(reply), INITIALIZE)
        elif reply[0] == REPLY_INIT:
            try:
                decode_init(reply, self.hardware)
            except FrameError as exc:
                log.warning("could not decode the INIT reply: %s", exc)

    async def _discover(self) -> None:
        hw = self.hardware
        firmware = hw.firmware
        if firmware is not None and not 1300 <= firmware <= 1999:
            log.warning("CLARIOstar firmware %.3f is outside 1.300-1.999; hardware options are not decoded", firmware / 1000)
            self.hardware = hw = Hardware(firmware=firmware, build=hw.build, board_revision=hw.board_revision)
        params = await self._command(REQUEST_PARAMETERS, expect=REPLY_PARAMETERS)
        if firmware is None or 1300 <= firmware <= 1999:
            hw.parameters = decode_parameters(params)
        version, built = decode_firmware_info(await self._command(REQUEST_FIRMWARE, expect=REPLY_FIRMWARE))
        stored = hw.parameters.get(158)
        if firmware is not None and firmware != version or stored is not None and stored != version:
            log.warning("CLARIOstar firmware versions disagree: INIT %s, info %s, parameters %s", firmware, version, stored)
        if version not in CONFIRMED_FIRMWARE:
            log.warning("CLARIOstar firmware %.2f is untested with this driver (confirmed: 1.35)", version / 1000)
        hw.firmware = hw.firmware or version
        self.identity = {"firmware": f"{version / 1000:.2f}", "firmware_built": built}
        if hw.parameters.get(154) or hw.parameters.get(156):
            self.identity["serial_number"] = f"{hw.parameters[154]:03d}-{hw.parameters[156]:04d}"
        extended = hw.parameters.get(134)
        if extended is not None and bool(extended) != self.extended_temperature:
            log.warning("the reader says its incubator is %s; the extended_temperature option says otherwise",
                        "extended (10-65 °C)" if extended else "standard (25-45 °C)")

    async def disconnect(self) -> None:
        try:
            status = await self._status()
            if self._running or status.busy and status.running == RUN:
                await self._abort_run()
            if self._target is not None or self._sensors_on:
                await self._command(temperature(TEMPERATURE_OFF))
                self._target, self._sensors_on = None, False
        except Exception as exc:  # leaving anyway; the link may already be gone
            log.warning("CLARIOstar shutdown: %s", exc)
        finally:
            await self.transport.close()

    # -- frames -------------------------------------------------------------------------------------

    async def _read(self, size: int, timeout_s: float) -> bytes:
        data = await self.transport.read_exactly(size, timeout_s)
        if len(data) != size:
            raise TimeoutError(f"{len(data)} of {size} bytes within {timeout_s:g}s")
        return bytes(data)

    async def _read_frame(self) -> bytes:
        """One frame, read by its length field (CR also occurs inside payloads). Stray bytes before STX are skipped."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + FRAME_CAP_S
        while (first := await self._read(1, self.reply_timeout_s))[0] != STX:
            if loop.time() > deadline:
                raise TimeoutError("no frame start")
        head = first + await self._read(2, self.silence_s)
        frame = bytearray(head)
        size = frame_size(head)
        while len(frame) < size:
            if loop.time() > deadline:
                raise TimeoutError(f"frame of {size} bytes not complete within {FRAME_CAP_S:g}s")
            chunk = min(CHUNK, size - len(frame))
            frame += await self._read(chunk, self.silence_s + chunk / LINE_BYTES_PER_S)
        return bytes(frame)

    async def _drain(self) -> None:
        """Drop input until the line has been quiet for ``silence_s``: the reader finishes any reply it started."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DRAIN_CAP_S
        while loop.time() < deadline:
            try:
                if not await self.transport.read_exactly(1, self.silence_s):
                    break
            except TimeoutError:
                break
            await self.transport.discard_input()
        await self.transport.discard_input()
        self._dirty = False

    async def _exchange(self, payload: bytes, *, attempts: int = 3) -> bytes:
        """Send one request and return the reply payload, retrying a missing or corrupt reply."""
        frame = encode(payload)
        action = ACTIONS.get(payload[0], f"command 0x{payload[0]:02X}")
        async with self._lock:
            try:
                if self._dirty:
                    await self._drain()
                failure: Exception | None = None
                for attempt in range(attempts):
                    if attempt:
                        await asyncio.sleep(RETRY_SETTLE_S)
                    await self.transport.discard_input()
                    await self.transport.write(frame)
                    try:
                        return decode(await self._read_frame())
                    except (FrameError, TimeoutError) as exc:
                        failure = exc
                        log.debug("CLARIOstar %s, attempt %d: %s", action, attempt + 1, exc)
                await self._drain()  # a late or partial reply must not be taken for the next one
            except asyncio.CancelledError:
                self._dirty = True
                raise
        if isinstance(failure, TimeoutError):
            raise DeviceTimeout(f"The CLARIOstar did not answer {action}.", details={"error": str(failure)})
        raise CommunicationError(f"Corrupt reply from the CLARIOstar to {action}: {failure}.")

    # -- commands and status --------------------------------------------------------------------------

    def _error(self, code: int | None, family: int) -> PyreagentError:
        action = ACTIONS.get(family, f"command 0x{family:02X}")
        reason = QUIT_CODES.get(code or 0, f"quit code {code}")
        message, details = f"The CLARIOstar refused {action}: {reason}", {"quit_code": code}
        if code in (1, 2, 3, 102, 213):
            return CommunicationError(message, details=details)
        if code in (55, 65, 110):
            return InvalidArgument(message, details=details)
        if code == 202:
            return DeviceBusy(message, details=details, suggested_action="Wait for the reader to finish, then retry.")
        if code == 243:
            return NeedsAttention(message, recoverable=True, details=details,
                                  suggested_action="Ask a human to load a plate (or use a lower focal height), then retry.")
        if code == 201:
            return HardwareError(message, details=details, suggested_action="Reset the device; reconnecting initializes the reader.")
        if code == 250:
            return HardwareError(message, details=details, suggested_action="Ask a human to check the reader for an obstruction, then reset.")
        return HardwareError(message, details=details)

    def _check(self, status: Status, family: int, benign: tuple[int, ...] = ()) -> None:
        if status.warning and status.codes != self._warned:
            log.warning("CLARIOstar warning %s: %s", status.codes, QUIT_CODES.get(status.quit_code or 0, "unknown"))
        self._warned = status.codes if status.warning else ()
        if status.refused and status.quit_code not in benign:
            raise self._error(status.quit_code, family)

    async def _status(self) -> Status:
        reply = await self._exchange(STATUS_REQUEST, attempts=5)
        if reply[0] != REPLY_STATUS:
            raise CommunicationError(f"Unexpected reply type 0x{reply[0]:02X} to a status request.")
        status = Status.decode(reply)
        # A refused flag in a STATUS reply belongs to an earlier command, except 201, which refuses STATUS itself.
        self._check(status, STATUS, benign=() if status.quit_code == 201 else (status.quit_code,))  # type: ignore[arg-type]
        return status

    async def _wait(self, done: Callable[[Status], bool], timeout_s: float, doing: str) -> Status:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while not done(status := await self._status()):
            if loop.time() > deadline:
                raise DeviceTimeout(f"The CLARIOstar was still {doing} after {timeout_s:g}s.")
            await asyncio.sleep(self.poll_interval_s)
        return status

    async def _command(self, payload: bytes, *, attempts: int = 3, idle_first: bool = True, expect: int | None = None,
                       benign: tuple[int, ...] = ()) -> bytes:
        """Send a command (after the reader is idle) and check the reply for a refusal."""
        if idle_first:
            await self._wait(lambda s: not s.busy, IDLE_TIMEOUT_S, "busy")
        reply = await self._exchange(payload, attempts=attempts)
        if reply[0] == REPLY_STATUS:
            self._check(Status.decode(reply), payload[0], benign)
        if expect is not None and reply[0] != expect:
            raise CommunicationError(f"Unexpected reply type 0x{reply[0]:02X} to {ACTIONS.get(payload[0], 'a command')}.")
        return reply

    # -- drawer ---------------------------------------------------------------------------------------

    async def _move_drawer(self, open_: bool) -> None:
        await self._command(drawer(open_), attempts=1)
        # Not busy alone: the open bit reaches its target slightly before the drawer comes to rest.
        await self._wait(lambda s: not s.busy and s.drawer_open == open_, DRAWER_TIMEOUT_S, "moving its drawer")

    async def open_tray(self) -> None:
        await self._move_drawer(True)

    async def close_tray(self) -> None:
        await self._move_drawer(False)

    async def tray_is_open(self) -> bool | None:
        return (await self._status()).drawer_open

    # -- measurements -----------------------------------------------------------------------------------

    def _plate(self, plate: Labware, wells: list[str]) -> tuple[PlateGeometry, list[tuple[int, int]], list[str]]:
        """Plate geometry in the reader's frame (y from the back edge), the cells to read, and their wells in read order."""
        geo = plate.require_geometry()
        if plate.rows * plate.columns > MAX_WELLS:
            raise InvalidArgument(f"{plate.id} has more than {MAX_WELLS} wells, which the CLARIOstar cannot be told to read.")
        if geo.size_z_mm > self.max_plate_height_mm:
            raise InvalidArgument(f"{plate.id} is {geo.size_z_mm:g} mm tall; this reader is set up for plates up to "
                                  f"{self.max_plate_height_mm:g} mm.",
                                  suggested_action="Use a lower plate, or raise max_plate_height_mm if the reader's manual allows it.")
        a1_from_back = geo.size_y_mm - geo.a1_y_mm  # labware y grows toward the back; the reader measures from the back edge
        geometry = PlateGeometry(
            size_x_mm=geo.size_x_mm, size_y_mm=geo.size_y_mm,
            a1_x_mm=geo.a1_x_mm, a1_y_mm=a1_from_back,
            last_x_mm=geo.a1_x_mm + (plate.columns - 1) * geo.pitch_x_mm,
            last_y_mm=a1_from_back + (plate.rows - 1) * geo.pitch_y_mm,
            columns=plate.columns, rows=plate.rows)
        cells = sorted({(row, column - 1) for row, column in map(parse_well, wells)})
        return geometry, cells, [f"{row_label(row)}{column + 1}" for row, column in cells]

    async def _measure(self, payload: bytes, layout: int, expected: int, estimate_s: float, progress: Progress) -> list[DataPage]:
        """Run a measurement and collect its data pages (``expected`` values in all)."""
        status = await self._wait(lambda s: not s.busy, IDLE_TIMEOUT_S, "busy")
        if status.drawer_open:
            raise NeedsAttention("The CLARIOstar's drawer is open.", recoverable=True, suggested_action="Call close_tray, then retry.")
        if self.require_plate and not status.plate_detected:
            raise NeedsAttention("The CLARIOstar does not detect a plate.", recoverable=True,
                                 suggested_action="Ask a human to load the plate, then retry (or set require_plate=False "
                                                  "if this plate type is not detected).")
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self.read_timeout_s + 2 * estimate_s
        pages: list[DataPage] = []
        received = 0
        self._running = True
        try:
            reply = await self._exchange(payload, attempts=1)
            if reply[0] == REPLY_STATUS and Status.decode(reply).refused:
                self._running = False
                self._check(Status.decode(reply), RUN)
            if reply[0] != REPLY_RUN_ACK:
                raise CommunicationError(f"Unexpected reply type 0x{reply[0]:02X} to a measurement.")
            while True:
                await asyncio.sleep(self.poll_interval_s)  # a poll sent right after the RUN reply goes unanswered
                status = await self._status()
                if status.data_ready and received < expected:
                    while received < expected:  # pages cannot be re-requested: one attempt each
                        page = decode_data(await self._command(REQUEST_DATA, attempts=1, idle_first=False))
                        if page.layout != layout or page.count == 0:
                            raise CommunicationError(f"Unexpected data page (layout 0x{page.layout:02X}, {page.count} values).")
                        pages.append(page)
                        received += page.count
                    if received != expected:
                        raise CommunicationError(f"The reader sent {received} values; expected {expected}.")
                    progress(1.0)
                elif not status.busy:
                    if received == expected:
                        return pages
                    if status.refused:
                        raise self._error(status.quit_code, RUN)
                    raise HardwareError("The CLARIOstar finished the measurement without sending data.")
                elif loop.time() > deadline:
                    raise DeviceTimeout(f"The measurement did not finish within {deadline - started:.0f}s.")
                else:
                    progress(min(0.95, (loop.time() - started) / max(estimate_s, 1.0)))
        except BaseException:
            if self._running:
                await self._abort_run()
            raise
        finally:
            self._running = False

    async def _abort_run(self) -> None:
        """Stop a measurement: drain whatever the reader is still sending, STOP, purge."""
        self._running = False
        self._dirty = True
        try:
            await self._command(STOP_REQUEST, idle_first=False, benign=(214,))
        except PyreagentError as exc:
            log.warning("CLARIOstar stop failed: %s", exc.message)
        await self.transport.discard_input()

    async def abort_read(self) -> None:
        if self._running:
            await self._abort_run()

    async def _absorbance(self, plate: Labware, wavelengths: list[int], wells: list[str], progress: Progress) -> dict[str, list[float]]:
        geometry, cells, ordered = self._plate(plate, wells)
        start = run_start(geometry, cells, mode=MODE_ABSORBANCE, scan=self.scan, settling_s=self.settling_s)
        spectrum = (wavelengths[0], wavelengths[-1], wavelengths[1] - wavelengths[0]) if len(wavelengths) > 1 else None
        payload = absorbance_run(start, wavelengths, flashes=self.absorbance_flashes, spectrum=spectrum)
        n, w = len(wavelengths), len(ordered)
        estimate = w * (0.5 + 0.012 * self.absorbance_flashes) + (0.005 * n * w if spectrum else 0.0)
        pages = await self._measure(payload, LAYOUT_ABSORBANCE, absorbance_values_expected(n, w), estimate, progress)
        od = optical_densities([v for page in pages for v in page.values], n, w)
        by_well = {well: [round(od[k][i], 4) for k in range(n)] for i, well in enumerate(ordered)}
        return {well: by_well[well] for well in wells}

    async def read_absorbance(self, *, plate: Labware, wavelength_nm: int, wells: list[str], progress: Progress) -> dict[str, float]:
        values = await self._absorbance(plate, [wavelength_nm], wells, progress)
        return {well: od[0] for well, od in values.items()}

    async def read_absorbance_spectrum(
        self, *, plate: Labware, wavelengths_nm: list[int], wells: list[str], progress: Progress
    ) -> dict[str, list[float]]:
        return await self._absorbance(plate, wavelengths_nm, wells, progress)

    async def read_fluorescence(
        self, *, plate: Labware, settings: FluorescenceSettings, wells: list[str], progress: Progress
    ) -> tuple[dict[str, float], int | None]:
        gain = self.default_gain if settings.gain is None else settings.gain
        flashes = settings.flashes or self.fluorescence_flashes
        focal = self.default_focal_height_mm if settings.focal_height_mm is None else settings.focal_height_mm
        excitation = band(settings.excitation_nm, self.excitation_bandwidth_nm)
        emission = band(settings.emission_nm, self.emission_bandwidth_nm)
        split = dichroic_split(excitation, emission)
        top_split = 760 if self._max_fluorescence_nm == 840 else 740
        if not 3400 <= split <= top_split * 10:
            raise InvalidArgument(f"The dichroic split ({split / 10:g} nm) falls outside 340-{top_split} nm.",
                                  suggested_action="Move the excitation and emission wavelengths into the reader's range.")
        geometry, cells, ordered = self._plate(plate, wells)
        start = run_start(geometry, cells, mode=MODE_FLUORESCENCE, bottom=settings.optics == "bottom", scan=self.scan,
                          settling_s=self.settling_s)
        payload = fluorescence_run(start, [channel_block(gain, excitation, emission, split)], focal_height_mm=focal, flashes=flashes)
        pages = await self._measure(payload, LAYOUT_FLUORESCENCE, len(ordered), len(ordered) * (0.5 + 0.012 * flashes), progress)
        counts, ceiling = [v for page in pages for v in page.values], pages[0].ceiling
        saturated = [well for well, count in zip(ordered, counts) if ceiling and count >= ceiling]
        if saturated:
            log.warning("CLARIOstar fluorescence saturated at gain %d in %s; lower the gain", gain, ", ".join(saturated))
        by_well = dict(zip(ordered, map(float, counts)))
        return {well: by_well[well] for well in wells}, gain

    async def read_luminescence(
        self, *, plate: Labware, integration_s: float, focal_height_mm: float | None, wells: list[str], progress: Progress
    ) -> dict[str, float]:
        geometry, cells, ordered = self._plate(plate, wells)
        start = run_start(geometry, cells, mode=MODE_LUMINESCENCE, edr=True, scan=self.scan, settling_s=0.0)
        focal = self.default_focal_height_mm if focal_height_mm is None else focal_height_mm
        payload = luminescence_run(start, focal_height_mm=focal, gain=self.luminescence_gain, emission=self.luminescence_band,
                                   normalization_s=self.luminescence_normalization_s, interval_s=integration_s)
        pages = await self._measure(payload, LAYOUT_LUMINESCENCE, len(ordered), len(ordered) * (integration_s + 0.5), progress)
        by_well = dict(zip(ordered, (float(v) for page in pages for v in page.values)))
        return {well: by_well[well] for well in wells}

    # -- incubation ---------------------------------------------------------------------------------------

    async def set_temperature(self, celsius: float) -> None:
        await self._command(temperature(setpoint(celsius)))
        self._target, self._sensors_on = celsius, True

    async def deactivate_temperature(self) -> None:
        # Monitor-only replaces the set point (one setting is active at a time) and keeps the sensors reporting.
        await self._command(temperature(TEMPERATURE_MONITOR))
        self._target, self._sensors_on = None, True

    async def get_temperature(self) -> tuple[float | None, float | None]:
        return (await self._status()).temperature_c, self._target

    def info(self) -> dict[str, Any]:
        hw = self.hardware
        return {**super().info(), **self.identity, "board_revision": hw.board_revision, "transport": self.transport.describe()}


__all__ = ["ClariostarBackend"]
