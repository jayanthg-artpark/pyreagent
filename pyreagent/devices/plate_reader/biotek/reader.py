"""Agilent BioTek microplate readers over their FTDI USB-serial port: Synergy H1, Cytation 1/5, Synergy HT.

Protocol knowledge comes from studying PyLabRobot's BioTek backends, their tests and their
captured fixtures (MIT); this is an independent implementation in pyreagent's driver model.

Every exchange is: one command letter, ACK (0x06; anything else is a rejection), then any
parameter bytes as a second write, then ``<data><4 hex digit status><ETX>``. Reads are
asynchronous: the start command answers ``0000`` at once and the result frame follows when the
plate is done. The reader is a COM port (FTDI's VCP driver) at 9600 baud, 8N2, RTS/CTS.

- Synergy H1, Cytation 1/5 (the reader part; no imaging), "RDOP22" protocol: absorbance at one
  wavelength (230-999 nm), monochromator fluorescence (top, 250-700 nm, automatic gain),
  luminescence, incubation, linear/orbital shaking, tray. A read is one ASCII assay record
  per rectangle of wells, so arbitrary well sets are covered by rectangles and merged. Plates
  need geometry (sent with ``y``). If identity times out the driver retries at 38461 baud.
- Synergy HT (basecode 7090202, tested firmware 2.24), legacy protocol: absorbance only, on
  the built-in 24- and 96-well layouts, reading the bounding rectangle of the requested wells.
  Whole-°C incubation within the limits the reader reports. Its status words carry a power-on
  flag until ``n 1`` is sent on connect, and it holds unsent replies across reconnects, so
  connect drains stale output first.

Limitations and unverified points (driver options where they matter):
- No gain, flash count, filter, bottom optics, spectrum or kinetic reads: the assay records
  carry Gen5 constants nobody has decoded (see ``protocol``).
- Luminescence integration is capped at 19.8 s in 0.2 s steps: Gen5's encoding overflows at 20 s.
- The RDOP22 setpoint encoding (°C x 1000) is unconfirmed on hardware. Temperature limits are
  defaults, not read from the device: H1 heats to 45 °C; Cytation incubation is off unless
  ``temperature_range_c`` is given (sources disagree on whether it works over this protocol).
  Setpoints below the current temperature are refused unless ``can_cool``.
- Instrument status codes have no known meanings, so every nonzero status is a hardware error
  carrying the code. A timed-out or cancelled read leaves the line in an unknown state: the
  driver sends abort (``x``) and drains stale output before the next command.
- The FTDI USB reset, DTR-off and latency settings the vendor software applies are not
  available through a COM port and are not sent. ``close_tray`` cannot send the plate height
  first (it does not know the plate); the geometry goes with each read instead.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Literal

from ....core.errors import CommunicationError, DeviceTimeout, HardwareError, InvalidArgument, NeedsAttention, NotSupported
from ....io import Transport, line_transport
from ....labware import Labware, parse_well
from ..backend import FluorescenceSettings, PlateReaderBackend, Progress
from ..models import AbsorbanceSpec, FluorescenceSpec, LuminescenceSpec, ReaderSpec, ShakeMode, ShakeSpec
from . import protocol as p

log = logging.getLogger("pyreagent.biotek")

Model = Literal["synergy_h1", "cytation1", "cytation5", "synergy_ht"]
MODEL_NAMES = {"synergy_h1": "Synergy H1", "cytation1": "Cytation 1", "cytation5": "Cytation 5", "synergy_ht": "Synergy HT"}
FOCAL_HEIGHT_MM = {"synergy_h1": (4.5, 10.68), "cytation1": (4.5, 13.88), "cytation5": (4.5, 13.88)}
H1_TEMPERATURE_C = (20.0, 45.0)

HT_BASECODE, HT_TESTED_VERSION = "7090202", "2.24"
HT_REQUIRED_CAPS = (1013, 1006, 1008, 1009, 1012, 1020)
CAP_RDOP22, CAP_ABS_FIELD, CAP_EXT_OPTICS, CAP_TRAY_SPEED, CAP_HT_TEMPERATURE = 2000, 2056, 2064, 2138, 1031

MAX_SHAKE_RECORD_S = 960
"""Firmware limit per shake record; longer shakes are renewed."""
MAX_INTEGRATION_S = 19.8
TIMEOUT_S = 20.0
QUICK_TIMEOUT_S = 1.0  # e, C, h, y
READ_TIMEOUT_S = {"absorbance": 180.0, "fluorescence": 120.0}


class BioTekBackend(PlateReaderBackend):
    vendor = "Agilent BioTek"

    def __init__(
        self,
        port: str | None = None,
        *,
        model: Model,
        baudrate: int = 9600,
        fallback_baudrate: int | None = 38461,
        host: str | None = None,
        tcp_port: int | None = None,
        temperature_range_c: tuple[float, float] | None = None,
        can_cool: bool = False,
        shake_amplitude_mm: int = 3,
        fluorescence_focal_height_mm: float = 7.5,
        luminescence_focal_height_mm: float = 4.5,
        well_blocks: Literal["exact", "bounding"] = "exact",
        slow_tray: bool = False,
        record_etx: bool = False,
        verify_result_checksum: bool = True,
        read_timeout_s: float | None = None,
        transport: Transport | None = None,
    ) -> None:
        """
        Args:
            port: COM port of the reader's FTDI USB-serial adapter (e.g. "COM5").
            model: "synergy_h1", "cytation1", "cytation5" or "synergy_ht"; checked against the reader on connect.
            baudrate: Line speed (9600 on every model).
            fallback_baudrate: H1/Cytation: speed to retry at if identity times out (None: don't).
            temperature_range_c: Incubation set-point range. H1 default 20-45 °C; Cytation: off unless given;
                HT: narrows the range the reader reports.
            can_cool: Allow set points below the current temperature (H1/Cytation; the H1 cannot cool).
            shake_amplitude_mm: H1/Cytation shake amplitude, 1-6 mm (1 mm ~ 1096 cpm ... 6 mm ~ 360 cpm).
            fluorescence_focal_height_mm, luminescence_focal_height_mm: Read heights used when a read gives none.
            well_blocks: "exact" covers the requested wells with rectangles (one read cycle each);
                "bounding" reads their bounding rectangle once. The HT always reads the bounding rectangle.
            slow_tray: Move the carrier slowly (needs reader support).
            record_etx: Append ETX after assay records (older Gen5 traffic did; the reader frames by length).
            verify_result_checksum: Check the RDOP22 result-frame checksum (rule inferred from captures).
            read_timeout_s: Per-rectangle result timeout; default 180 s absorbance, 120 s fluorescence,
                60 s + 130 s per whole second of luminescence integration.
        """
        if model not in MODEL_NAMES:
            raise ValueError(f"model must be one of {sorted(MODEL_NAMES)}")
        if not 1 <= shake_amplitude_mm <= 6:
            raise ValueError("shake_amplitude_mm must be 1..6")
        self.reader_model = model
        self.legacy = model == "synergy_ht"
        self.transport = transport or line_transport(port=port, host=host, tcp_port=tcp_port, baudrate=baudrate,
                                                     bytesize=8, parity="N", stopbits=2, rtscts=True)
        self.fallback_baudrate = None if self.legacy else fallback_baudrate
        self.temperature_range_c = temperature_range_c
        self.can_cool = can_cool
        self.shake_amplitude_mm = shake_amplitude_mm
        self.focal_height_mm = {"fluorescence": fluorescence_focal_height_mm, "luminescence": luminescence_focal_height_mm}
        self.well_blocks = well_blocks
        self.slow_tray = slow_tray
        self.record_etx = record_etx
        self.verify_result_checksum = verify_result_checksum
        self.read_timeout_s = read_timeout_s
        self.ack_only_pause_s = 0.25
        self.drain_poll_s, self.drain_quiet_reads, self.drain_limit_s = 0.05, 10, 10.0
        self.identity: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._caps: dict[int, tuple[bool, str]] = {}
        self._ht_limits_c: tuple[float, float] | None = None
        self._geometry: bytes | None = None
        self._tray_speed: bool | None = None
        self._tray_open: bool | None = None
        self._target_c: float | None = None
        self._last_c: float | None = None
        self._stale = False  # the line may hold late output (a read or exchange did not finish cleanly)
        self._running = False  # a read or shake was started and has not finished cleanly

    @property
    def model(self) -> str:  # type: ignore[override]
        return MODEL_NAMES[self.reader_model]

    @property
    def spec(self) -> ReaderSpec:  # type: ignore[override]
        if self.legacy:
            limits = self._ht_limits_c
            if limits and self.temperature_range_c:
                limits = (max(limits[0], self.temperature_range_c[0]), min(limits[1], self.temperature_range_c[1]))
            return ReaderSpec(absorbance=AbsorbanceSpec(min_nm=200, max_nm=999), temperature_c=limits,
                              plate_formats=sorted(p.HT_PRESETS))
        heights = FOCAL_HEIGHT_MM[self.reader_model]
        temperature = self.temperature_range_c or (H1_TEMPERATURE_C if self.reader_model == "synergy_h1" else None)
        return ReaderSpec(
            absorbance=AbsorbanceSpec(min_nm=230, max_nm=999),
            fluorescence=FluorescenceSpec(min_excitation_nm=250, max_excitation_nm=700, min_emission_nm=250, max_emission_nm=700,
                                          optics=["top"], focal_height_mm=heights),
            luminescence=LuminescenceSpec(min_integration_s=0.2, max_integration_s=MAX_INTEGRATION_S, focal_height_mm=heights),
            temperature_c=temperature,
            shaking=ShakeSpec(modes=["linear", "orbital"], max_duration_s=3600),
            needs_geometry=True,
        )

    # -- connection ---------------------------------------------------------------------------------

    async def connect(self) -> None:
        self._caps.clear()
        self._geometry = self._tray_speed = self._tray_open = self._target_c = None
        self._stale = self._running = False
        await self.transport.open()
        try:
            async with self._lock:
                await (self._connect_legacy() if self.legacy else self._connect_rdop22())
        except BaseException:
            await self.transport.close()
            raise

    async def _connect_rdop22(self) -> None:
        try:
            identity = await self._command("e", timeout_s=QUICK_TIMEOUT_S)
        except DeviceTimeout:
            settings = getattr(self.transport, "settings", None)
            if not self.fallback_baudrate or not isinstance(settings, dict) or "baudrate" not in settings:
                raise
            log.info("no identity at %s baud; retrying at %s", settings["baudrate"], self.fallback_baudrate)
            settings["baudrate"] = self.fallback_baudrate
            await self.transport.close()
            await self.transport.open()
            identity = await self._command("e", timeout_s=QUICK_TIMEOUT_S)
        self._identify(identity)
        if self.identity["basecode"] == HT_BASECODE or not (await self._capability(CAP_RDOP22))[0]:
            raise NeedsAttention(f"This reader ({self.identity['basecode']}) speaks the Synergy HT protocol, not the {self.model}'s.",
                                 suggested_action="Set the driver's model to synergy_ht.")

    async def _connect_legacy(self) -> None:
        await self.transport.discard_input()
        await self._drain()
        self._identify(await self._command("e", timeout_s=QUICK_TIMEOUT_S))
        if self.identity["basecode"] != HT_BASECODE:
            raise NeedsAttention(f"This reader (basecode {self.identity['basecode']}) is not a Synergy HT.",
                                 suggested_action="Set the driver's model to the reader's model (synergy_h1, cytation1 or cytation5).")
        if self.identity["version"] != HT_TESTED_VERSION:
            log.warning("Synergy HT firmware %s; only %s has been tested", self.identity["version"], HT_TESTED_VERSION)
        serial = (await self._command("C", timeout_s=QUICK_TIMEOUT_S)).decode("ascii", "replace").strip()
        if not serial.isdigit():
            raise CommunicationError(f"Unrecognised serial number {serial!r}.")
        self.identity["serial_number"] = serial
        if (await self._capability(CAP_RDOP22))[0]:
            raise NeedsAttention("This reader speaks the RDOP22 protocol, not the Synergy HT's.",
                                 suggested_action="Set the driver's model to the reader's model (synergy_h1, cytation1 or cytation5).")
        for number in HT_REQUIRED_CAPS:
            if not (await self._capability(number))[0]:
                raise NotSupported(f"This Synergy HT lacks capability {number}, which the driver relies on.")
        supported, detail = await self._capability(CAP_HT_TEMPERATURE)
        self._ht_limits_c = None
        if supported:
            fields = detail.split(",")
            low, high = int(fields[0]), int(fields[1])
            if not 0 < low <= high <= 99:
                raise CommunicationError(f"Implausible incubation limits {detail!r}.")
            self._ht_limits_c = (float(low), float(high))
        await self._command("n", b"1")  # clears the power-on flag and enables result status

    def _identify(self, data: bytes) -> None:
        basecode, version = p.parse_identity(data)
        self.identity.update(basecode=basecode, version=version)

    async def disconnect(self) -> None:
        if self._running:
            await self._abort()
        await self.transport.close()

    # -- exchanges ----------------------------------------------------------------------------------

    async def _read(self, size: int | None, deadline: float, what: str) -> bytes:
        """``size`` bytes, or (None) up to ETX, before ``deadline``."""
        remaining = max(deadline - asyncio.get_running_loop().time(), 0.001)
        try:
            if size is None:
                return await self.transport.read_until(p.ETX, remaining)
            return await self.transport.read_exactly(size, remaining)
        except TimeoutError:
            self._stale = True
            raise DeviceTimeout(f"No reply from the reader to {what}.") from None

    def _status(self, reply: bytes, what: str, *, power_on_flag: bool = False) -> bytes:
        data, code = p.split_status(reply, power_on_flag=self.legacy or power_on_flag)
        if code:
            raise HardwareError(f"The reader reports error 0x{code:04X} for {what}.", details={"status": f"{code:04X}"},
                                suggested_action="Ask a human to check the reader (tray, plate, lid) and its display, then reset it.")
        return data

    async def _command(self, command: str, param: bytes = b"", *, size: int | None = None, ack_only: bool = False,
                       timeout_s: float = TIMEOUT_S) -> bytes:
        """One exchange; returns the reply data. ``size``: fixed data length (binary-safe) instead of reading to ETX."""
        deadline = asyncio.get_running_loop().time() + timeout_s
        what = f"{command!r}"
        if self._stale:
            await self._drain()
        if not self.legacy:
            await self.transport.discard_input()
        await self.transport.write(command.encode("ascii"))
        ack = await self._read(1, deadline, what)
        if ack != p.ACK:
            self._stale = True
            raise CommunicationError(f"The reader rejected command {what} ({'NAK' if ack == p.NAK else repr(ack)}).")
        if param:
            await self.transport.write(param)
        if ack_only:
            await asyncio.sleep(self.ack_only_pause_s)
            return b""
        reply = await self._read(None if size is None else size + 5, deadline, what)
        return self._status(reply, what, power_on_flag=command == "e")  # so a Synergy HT is recognised as one

    async def _drain(self) -> None:
        """Discard output until the line has been quiet for a while (late replies, frames from an aborted read)."""
        loop = asyncio.get_running_loop()
        give_up = loop.time() + self.drain_limit_s
        quiet = dropped = 0
        while quiet < self.drain_quiet_reads:
            if loop.time() > give_up:
                raise CommunicationError(f"The reader kept sending data for {self.drain_limit_s:g}s; it may still be measuring.")
            try:
                await self.transport.read_exactly(1, self.drain_poll_s)
                quiet, dropped = 0, dropped + 1
            except TimeoutError:
                quiet += 1
        if dropped:
            log.info("discarded %d stale bytes from the reader", dropped)
        self._stale = False

    async def _capability(self, number: int) -> tuple[bool, str]:
        if number not in self._caps:
            data = await self._command("z", f"{number:04d}".encode() + p.ETX, size=17)
            self._caps[number] = p.parse_capability(data)
        return self._caps[number]

    async def _abort(self) -> None:
        """Abort a read or shake (no reply); late output is drained before the next command."""
        await self.transport.write(b"x")
        self._running = False
        self._stale = True

    # -- tray ---------------------------------------------------------------------------------------

    async def _set_tray_speed(self) -> None:
        if self.legacy or self._tray_speed == self.slow_tray:
            return
        if not (await self._capability(CAP_TRAY_SPEED))[0]:
            if self.slow_tray:
                raise NotSupported(f"This {self.model} cannot move its carrier slowly.",
                                   suggested_action="Set the driver option slow_tray to false.")
        else:
            await self._command("&", b"S1" if self.slow_tray else b"S0")
        self._tray_speed = self.slow_tray

    async def open_tray(self) -> None:
        async with self._lock:
            await self._set_tray_speed()
            await self._command("J")
            self._tray_open = True

    async def close_tray(self) -> None:
        async with self._lock:
            await self._set_tray_speed()
            await self._command("A")
            self._tray_open, self._geometry = False, None  # the plate may have changed

    async def tray_is_open(self) -> bool | None:
        return self._tray_open

    # -- reads (RDOP22) -----------------------------------------------------------------------------

    def _blocks(self, wells: list[str]) -> list[p.Rect]:
        cells = {(r + 1, c) for r, c in map(parse_well, wells)}  # parse_well: 0-based row, 1-based column
        if self.legacy or self.well_blocks == "bounding":
            return [p.bounding(cells)]
        return p.cover(cells)

    async def _load_geometry(self, plate: Labware) -> None:
        param = p.geometry(plate.rows, plate.columns, plate.require_geometry())
        if param != self._geometry:
            await self._command("y", param, timeout_s=QUICK_TIMEOUT_S)
            self._geometry = param

    async def _read_blocks(self, bodies: list[tuple[p.Rect, str]], timeout_s: float, wells: list[str], progress: Progress,
                           ) -> dict[str, float]:
        """Run one ``D``/``O``/result cycle per rectangle and keep the requested wells."""
        values: dict[tuple[int, int], float] = {}
        done = 0
        for rect, body in bodies:
            reply = await self._command("D", p.record(body, etx=self.record_etx))
            p.parse_record_reply(reply)
            values |= await self._start_and_collect(timeout_s)
            done += (rect[2] - rect[0] + 1) * (rect[3] - rect[1] + 1)
            progress(min(done / len(wells), 1.0))
        return self._pick(values, wells)

    async def _start_and_collect(self, timeout_s: float) -> dict[tuple[int, int], float]:
        if await self._command("O"):
            raise CommunicationError("Unexpected data in the reply to the start command.")
        self._running = True
        frame = await self._read(None, asyncio.get_running_loop().time() + timeout_s, "the read (result frame)")
        self._running = False
        result = p.parse_result(self._status(frame, "the read"), verify_checksum=self.verify_result_checksum)
        self._last_c = result.temperature_c if result.temperature_c is not None else self._last_c
        return result.values

    @staticmethod
    def _pick(values: dict[tuple[int, int], float], wells: list[str]) -> dict[str, float]:
        picked: dict[str, float] = {}
        for well in wells:
            r, c = parse_well(well)
            if (r + 1, c) not in values:
                raise CommunicationError(f"The reader returned no value for {well}.")
            picked[well] = values[(r + 1, c)]
        return picked

    async def _measure(self, run: Any) -> Any:
        """Hold the line for a whole read; abort it on cancel or timeout."""
        async with self._lock:
            try:
                return await run()
            except (asyncio.CancelledError, DeviceTimeout):
                if self._running:
                    await asyncio.shield(self._abort())
                raise

    async def read_absorbance(self, *, plate: Labware, wavelength_nm: int, wells: list[str], progress: Progress) -> dict[str, float]:
        if self.legacy:
            return await self._measure(lambda: self._read_ht(plate, wavelength_nm, wells, progress))

        async def run() -> dict[str, float]:
            extra, extended = (await self._capability(CAP_ABS_FIELD))[0], (await self._capability(CAP_EXT_OPTICS))[0]
            await self._load_geometry(plate)
            bodies = [(rect, p.absorbance_body(rect, wavelength_nm, extra_field=extra, extended_optics=extended))
                      for rect in self._blocks(wells)]
            return await self._read_blocks(bodies, self.read_timeout_s or READ_TIMEOUT_S["absorbance"], wells, progress)
        return await self._measure(run)

    async def read_fluorescence(
        self, *, plate: Labware, settings: FluorescenceSettings, wells: list[str], progress: Progress
    ) -> tuple[dict[str, float], int | None]:
        if settings.flashes is not None:
            raise InvalidArgument(f"The {self.model} driver cannot set the flash count; omit flashes.")
        height = settings.focal_height_mm or self.focal_height_mm["fluorescence"]

        async def run() -> dict[str, float]:
            await self._load_geometry(plate)
            await self._command("t", p.focus(p.FOCUS_FLUORESCENCE, height))
            bodies = [(rect, p.fluorescence_body(rect, settings.excitation_nm, settings.emission_nm)) for rect in self._blocks(wells)]
            return await self._read_blocks(bodies, self.read_timeout_s or READ_TIMEOUT_S["fluorescence"], wells, progress)
        return await self._measure(run), None

    async def read_luminescence(
        self, *, plate: Labware, integration_s: float, focal_height_mm: float | None, wells: list[str], progress: Progress
    ) -> dict[str, float]:
        if not math.isclose(integration_s * 5, round(integration_s * 5), abs_tol=1e-6):
            raise InvalidArgument(f"integration_s must be a multiple of 0.2 s on the {self.model}.")
        height = focal_height_mm or self.focal_height_mm["luminescence"]

        async def run() -> dict[str, float]:
            await self._load_geometry(plate)
            await self._command("t", p.focus(p.FOCUS_LUMINESCENCE, height))
            bodies = [(rect, p.luminescence_body(rect, integration_s)) for rect in self._blocks(wells)]
            timeout = self.read_timeout_s or 60 + int(integration_s) * 130
            return await self._read_blocks(bodies, timeout, wells, progress)
        return await self._measure(run)

    async def abort_read(self) -> None:
        if self._running:
            await self._abort()

    # -- reads (Synergy HT) -------------------------------------------------------------------------

    async def _read_ht(self, plate: Labware, wavelength_nm: int, wells: list[str], progress: Progress) -> dict[str, float]:
        r0, c0, r1, c1 = self._blocks(wells)[0]
        await self._command("V", p.ht_setup_packet(wavelength_nm))
        await self._command("{", bytes([p.HT_PRESETS[(plate.rows, plate.columns)]]))
        await self._command("i", b"1", ack_only=True)
        await self._command("r", bytes([r0, r1]))
        await self._command("%", bytes([c0, c1]))
        await self._command("k", b"0")
        await self._command("Q", b"1", ack_only=True)
        await self._command("t", p.HT_FOCUS)
        if await self._command("S"):
            raise CommunicationError("Unexpected data in the reply to the start command.")
        self._running = True
        rows, columns = r1 - r0 + 1, c1 - c0 + 1
        deadline = asyncio.get_running_loop().time() + (self.read_timeout_s or READ_TIMEOUT_S["absorbance"])
        first = await self._read(1, deadline, "the read (result frame)")
        if first != p.CR:  # short error frame: 0x10 or ETX, then a status word
            self._running = False
            self._status(await self._read(5, deadline, "the read"), "the read")
            raise CommunicationError(f"Unexpected result frame start {first!r}.")
        frame = first + await self._read(p.ht_frame_length(rows, columns) - 1, deadline, "the read (result frame)")
        self._running = False
        self._status(frame[-5:], "the read")
        table, temperature = p.parse_ht_result(frame, rows, columns)
        self._last_c = temperature if temperature is not None else self._last_c
        progress(1.0)
        values = {(r0 + i, c0 + j): value for i, row in enumerate(table) for j, value in enumerate(row)}
        return self._pick(values, wells)

    # -- incubation ---------------------------------------------------------------------------------

    async def _current(self) -> float | None:
        data = await self._command("h", size=3, timeout_s=QUICK_TIMEOUT_S)
        self._last_c = p.parse_temperature(data)
        return self._last_c

    async def set_temperature(self, celsius: float) -> None:
        async with self._lock:
            if self.legacy:
                if celsius != int(celsius):
                    raise InvalidArgument("The Synergy HT incubates in whole °C.")
                await self._command("g", f"{int(celsius):2d}".encode())
            else:
                current = await self._current()
                if not self.can_cool and current is not None and celsius < current:
                    raise InvalidArgument(f"The {self.model} can only heat; {celsius:g} °C is below the current {current:.1f} °C.")
                await self._command("g", f"{round(celsius * 1000):05d}".encode())
            self._target_c = celsius

    async def deactivate_temperature(self) -> None:
        async with self._lock:
            await self._command("g", b" 0" if self.legacy else b"00000")
            self._target_c = None

    async def get_temperature(self) -> tuple[float | None, float | None]:
        if self._lock.locked():  # a read or shake holds the line; report the last known value
            return self._last_c, self._target_c
        async with self._lock:
            return await self._current(), self._target_c

    # -- shaking ------------------------------------------------------------------------------------

    async def _wait(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def shake(self, *, mode: ShakeMode, rpm: int | None, duration_s: float) -> None:
        if self.legacy:
            raise self._unsupported("shake")
        remaining = max(1, round(duration_s))
        async with self._lock:
            try:
                while remaining > 0:
                    chunk = min(remaining, MAX_SHAKE_RECORD_S)
                    body = p.shake_body(chunk, orbital=mode == "orbital", amplitude_mm=self.shake_amplitude_mm)
                    reply = await self._command("D", p.record(body, etx=self.record_etx))
                    if reply:
                        p.parse_record_reply(reply)
                    if await self._command("O"):
                        raise CommunicationError("Unexpected data in the reply to the start command.")
                    self._running = True
                    await self._wait(chunk)
                    remaining -= chunk
                self._running = False
                self._stale = True  # whatever the reader sends when a shake ends is unknown; drop it
            except asyncio.CancelledError:
                if self._running:
                    await asyncio.shield(self._abort())
                raise

    async def stop_shaking(self) -> None:
        if self._running:
            await self._abort()

    def info(self) -> dict[str, Any]:
        return {**super().info(), **self.identity, **self.transport.describe()}


__all__ = ["BioTekBackend"]
