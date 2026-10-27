"""Tecan Infinite 200 PRO (M series, monochromator) multimode reader over USB bulk.

Protocol knowledge comes from studying PyLabRobot's experimental Infinite 200 PRO backend (MIT),
itself reverse-engineered from USB captures of Tecan's own software; this is an independent
implementation in pyreagent's driver model.

Commands are ASCII in a small binary frame (see ``protocol``). The reader answers ``ST`` (done),
``+`` (acknowledged), ``-`` (rejected), or ``BY#T<ms>`` (busy for about that long; ``ST`` follows)
first. Queries (``?...``/``#...``) answer with exactly one value frame. Measurements stream one
binary packet per well plus a calibration packet that can arrive at any time, even after the last
row, so a run buffers every packet and converts once the run is over.

No plate definition is sent: each row is a pair of absolute stage moves and ``SCANX`` over evenly
spaced points. Positions come from the labware geometry, measured from the plate's A1 corner
(x toward higher columns, y toward later rows), in stage counts per mm (driver options; the
1000/mm default is unconfirmed). ``SCANX`` cannot skip wells, so a row is always measured from
its first to its last requested column and only the requested wells are kept.

Supported: absorbance at one wavelength (230-1000 nm, OD clamped at 4), top fluorescence
(230-850 nm, manual gain 0-255; no gain given means ``default_gain``), luminescence, plate carrier
in/out, plate incubation (0-42 °C commanded; the instrument only heats, to ambient + 5 °C or more),
and timed linear or orbital shaking at a fixed amplitude (no rpm setting; frequency follows the
amplitude). Not supported: bottom fluorescence, automatic gain, spectra, the F series.

Limitations: only absorbance needs its calibration packet; fluorescence and luminescence fall back
to uncorrected values without one (logged). The replies to the step-loss and lid checks have an
unknown format; only explicit ``ERR`` replies are acted on. A shake cannot be stopped once
accepted. A timed-out query does not trigger an automatic USB reset; use the device's reset.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any, Literal

from ....core.errors import CommunicationError, DeviceTimeout, HardwareError, NeedsAttention
from ....io import UsbBulkTransport
from ....labware import Labware, parse_well, row_label
from ..backend import FluorescenceSettings, PlateReaderBackend, Progress
from ..models import AbsorbanceSpec, FluorescenceSpec, LuminescenceSpec, ReaderSpec, ShakeMode, ShakeSpec
from .protocol import (
    PRODUCT_ID,
    VENDOR_ID,
    AbsorbanceDecoder,
    CalibrationError,
    Decoder,
    FluorescenceDecoder,
    FrameParser,
    LuminescenceDecoder,
    busy_ms,
    decode_status,
    encode,
    is_terminal,
    parse_capability,
)

log = logging.getLogger("pyreagent.tecan.infinite")

SHAKE_MODES: dict[str, str] = {"orbital": "ORBITAL", "linear": "LINEAR"}
DEFAULT_ABS_BEAM = 700  # used when the reader does not report #BEAM DIAMETER
FI_BEAM = 3000
USER_CHECKS = ("CHECK LUM.FIBER", "CHECK LUM.LID")  # an error here means the setup, not the instrument


@dataclass(frozen=True)
class RowScan:
    row: int
    columns: list[int]
    """0-based, in the order the reader visits them."""
    y: int
    x_start: int
    x_end: int


def _clears(excitation: bool, emission: bool) -> list[str]:
    return [*(["EXCITATION CLEAR"] if excitation else []), *(["EMISSION CLEAR"] if emission else []),
            "TIME CLEAR", "GAIN CLEAR", "READS CLEAR", "POSITION CLEAR", "MIRROR CLEAR"]


class TecanInfiniteBackend(PlateReaderBackend):
    vendor = "Tecan"
    model = "Infinite 200 PRO"
    spec = ReaderSpec(
        absorbance=AbsorbanceSpec(min_nm=230, max_nm=1000),
        fluorescence=FluorescenceSpec(min_excitation_nm=230, max_excitation_nm=850, min_emission_nm=230, max_emission_nm=850,
                                      min_gain=0, max_gain=255, optics=["top"], focal_height_mm=(0.0, 30.0)),
        luminescence=LuminescenceSpec(min_integration_s=0.001, max_integration_s=60.0, focal_height_mm=(0.0, 30.0)),
        temperature_c=(0.0, 42.0),
        shaking=ShakeSpec(modes=["orbital", "linear"], min_rpm=None, max_duration_s=999),
        tray=True,
        needs_geometry=True,
    )

    def __init__(
        self,
        *,
        serial_number: str | None = None,
        counts_per_mm_x: float = 1000.0,
        counts_per_mm_y: float = 1000.0,
        counts_per_mm_z: float = 1000.0,
        x_offset_mm: float = 0.0,
        y_offset_mm: float = 0.0,
        flashes: int = 25,
        default_gain: int = 100,
        default_focal_height_mm: float = 20.0,
        absorbance_bandwidth_nm: float | None = None,
        excitation_bandwidth_nm: float = 5.0,
        emission_bandwidth_nm: float = 20.0,
        integration_us: int = 20,
        lag_us: int = 0,
        luminescence_dark_s: float = 3.0,
        shake_amplitude_mm: float = 1.0,
        strict_setup: bool = True,
        reply_timeout_s: float = 30.0,
        init_timeout_s: float = 120.0,
        tray_timeout_s: float = 10.0,
        row_timeout_s: float = 300.0,
        packet_timeout_s: float = 3.0,
        transport: Any = None,
    ) -> None:
        """
        Args:
            serial_number: USB serial number, needed when several Infinites are plugged in.
            counts_per_mm_x, counts_per_mm_y, counts_per_mm_z: stage counts per mm (unconfirmed for real hardware).
            x_offset_mm, y_offset_mm: added to every well position, in the reader's frame (x toward higher
                columns, y from the plate's back edge toward the front).
            flashes: flashes (absorbance, fluorescence) or reads (luminescence) per well.
            default_gain: fluorescence gain when a read does not give one (the reader has no automatic gain).
            default_focal_height_mm: fluorescence and luminescence read height when a read does not give one.
            absorbance_bandwidth_nm: monochromator bandwidth; default 9 nm above 315 nm, else 5 nm.
            excitation_bandwidth_nm, emission_bandwidth_nm, integration_us, lag_us: fluorescence optics.
            luminescence_dark_s: dark (reference) integration before each luminescence read.
            shake_amplitude_mm: 1.0 to 6.0 in 0.5 mm steps; the reader derives the frequency from it.
            strict_setup: raise when the reader rejects (``-``) a measurement setup command. Turn off only
                if a firmware is found to reject a command its own software sends.
            reply_timeout_s: wait for a command reply; a busy reply extends it to the announced time.
            init_timeout_s: wait for INIT on connect (about 80 s observed).
            tray_timeout_s: wait for a carrier move to finish once it has started.
            row_timeout_s: longest one plate row may stream.
            packet_timeout_s: one USB read; also the quiet period that ends a drain of stale input.
            transport: inject a transport (tests use a fake).
        """
        if not (1.0 <= shake_amplitude_mm <= 6.0 and (shake_amplitude_mm * 2).is_integer()):
            raise ValueError("shake_amplitude_mm must be 1.0 to 6.0 in 0.5 mm steps")
        self.transport = transport or UsbBulkTransport(VENDOR_ID, PRODUCT_ID, serial_number=serial_number, read_size=512,
                                                       timeout_s=reply_timeout_s, zero_length_packets=True)
        self.counts_per_mm = (counts_per_mm_x, counts_per_mm_y, counts_per_mm_z)
        self.offset_mm = (x_offset_mm, y_offset_mm)
        self.flashes = max(1, flashes)
        self.default_gain = default_gain
        self.default_focal_height_mm = default_focal_height_mm
        self.absorbance_bandwidth_nm = absorbance_bandwidth_nm
        self.excitation_bandwidth_nm, self.emission_bandwidth_nm = excitation_bandwidth_nm, emission_bandwidth_nm
        self.integration_us, self.lag_us = integration_us, lag_us
        self.luminescence_dark_s = luminescence_dark_s
        self.shake_amplitude_um = round(shake_amplitude_mm * 1000)
        self.strict_setup = strict_setup
        self.reply_timeout_s, self.init_timeout_s, self.tray_timeout_s = reply_timeout_s, init_timeout_s, tray_timeout_s
        self.row_timeout_s, self.packet_timeout_s = row_timeout_s, packet_timeout_s
        self.beam_diameter = DEFAULT_ABS_BEAM
        self.status_text: str | None = None
        self._io = asyncio.Lock()
        self._parser = FrameParser()
        self._stale = False  # an exchange was interrupted; drain before the next one
        self._busy_until = 0.0
        self._decoder: Decoder | None = None
        self._run_checks: list[str] = []
        self._wells_seen = 0
        self._wells_total = 0
        self._progress: Progress = lambda f: None
        self._tray_open: bool | None = None
        self._temperature: tuple[float | None, float | None] = (None, None)

    # -- connection ---------------------------------------------------------------------------------

    async def connect(self) -> None:
        await self.transport.open()
        try:
            async with self._io:
                self._stale = True
                await self._resync()
            try:
                frames = await self._exchange("QQ", until="first")
                self.status_text = frames[-1]
                if decode_status(self.status_text) in ("message", "unknown"):
                    log.warning("Infinite status on connect: %s", self.status_text)
            except DeviceTimeout:
                log.warning("no reply to QQ; initialising anyway")
            frames = await self._exchange("INIT FORCE", completion_s=self.init_timeout_s, mutating=True)
            self._check("INIT FORCE", frames)
            try:
                await self._exchange("MODE ABS")
                frames = await self._exchange("#BEAM DIAMETER", until="first")
                self.beam_diameter = parse_capability(frames[-1], DEFAULT_ABS_BEAM)
            except DeviceTimeout:
                log.warning("no absorbance beam diameter reported; using %d", DEFAULT_ABS_BEAM)
        except BaseException:
            await self.transport.close()
            raise

    async def disconnect(self) -> None:
        commands = ["TERMINATE", *self._run_checks] if self._decoder is not None else []
        try:
            for command in [*commands, "KEYLOCK OFF", "ABSOLUTE MTP,IN"]:  # replies are not awaited
                try:
                    await self.transport.write(encode(command))
                except Exception as exc:
                    log.debug("%s on disconnect: %s", command, exc)
        finally:
            await self.transport.close()

    # -- exchanges ----------------------------------------------------------------------------------

    async def _read_chunk(self, timeout_s: float) -> bytes:
        """One non-empty USB transfer. Raises ``TimeoutError`` after ``timeout_s``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while (remaining := deadline - loop.time()) > 0:
            try:
                data = await self.transport.read(min(self.packet_timeout_s, remaining))
            except TimeoutError:
                continue
            if data:
                return data
        raise TimeoutError

    def _feed(self, chunk: bytes, *, bare_ascii: bool) -> list[str]:
        frames = []
        for item in self._parser.feed(chunk, bare_ascii=bare_ascii):
            if isinstance(item, str):
                frames.append(item)
                if (ms := busy_ms(item)) is not None:
                    self._busy_until = asyncio.get_running_loop().time() + ms / 1000
            elif self._decoder is None:
                log.debug("ignoring a %d-byte binary packet outside a measurement", len(item))
            else:
                kind = self._decoder.add(item)
                if kind == "data":
                    self._wells_seen += 1
                    self._progress(min(1.0, self._wells_seen / max(1, self._wells_total)))
                elif kind == "unknown":
                    log.warning("unrecognised %d-byte measurement packet ignored", len(item))
        return frames

    async def _resync(self) -> None:
        """Discard replies an interrupted exchange left behind (e.g. the late ``ST`` of a cancelled shake).

        Within a run, binary packets still go to the decoder: the calibration packet may be among them."""
        in_run = self._decoder is not None
        if not in_run:
            self._parser.reset()
        loop = asyncio.get_running_loop()
        while True:
            wait = max(self._busy_until - loop.time(), 0.0) + self.packet_timeout_s
            try:
                chunk = await self.transport.read(wait)
            except TimeoutError:
                break
            if any(is_terminal(f) for f in self._feed(chunk, bare_ascii=False)):
                self._busy_until = 0.0
        if not in_run:
            self._parser.reset()
        self._stale = False

    async def _collect(self, command: str, *, first_only: bool = False, completion_s: float | None = None,
                       mutating: bool = False) -> list[str]:
        """Read reply frames until a terminal one (or, with ``first_only``, any frame)."""
        frames: list[str] = []
        timeout = self.reply_timeout_s
        self._stale = True
        while True:
            try:
                chunk = await self._read_chunk(timeout)
            except TimeoutError:
                raise DeviceTimeout(f"The reader did not {'finish' if frames else 'answer'} {command!r}.",
                                    faults_device=mutating, details={"command": command, "replies": frames}) from None
            for frame in self._feed(chunk, bare_ascii=True):
                frames.append(frame)
                if (ms := busy_ms(frame)) is not None:
                    timeout = max(completion_s or self.reply_timeout_s, math.ceil(ms / 1000), 1)
            if self._parser.binary_pending:
                continue
            if (first_only and frames) or any(is_terminal(f) for f in frames):
                self._stale = False
                self._busy_until = 0.0
                return frames

    async def _exchange(self, command: str, *, until: Literal["terminal", "first"] = "terminal",
                        completion_s: float | None = None, mutating: bool = False) -> list[str]:
        async with self._io:
            if self._stale:
                await self._resync()
            await self.transport.write(encode(command))
            return await self._collect(command, first_only=until == "first", completion_s=completion_s, mutating=mutating)

    def _check(self, command: str, frames: list[str]) -> None:
        """Raise for a rejection (``-``) or a device error (``ERR...``) among ``frames``."""
        for frame in frames:
            if frame.startswith("ERR"):
                details = {"command": command, "reply": frame}
                if command in USER_CHECKS:
                    raise NeedsAttention(f"The reader failed {command!r}: {frame}", recoverable=True, details=details,
                                         suggested_action="Ask a human to check the lid and luminescence fibre are in place, then retry.")
                raise HardwareError(f"The reader reports {frame!r} (after {command!r}).", details=details)
            if frame == "-":
                raise CommunicationError(f"The reader rejected {command!r}.", details={"command": command},
                                         suggested_action="Check the reader is idle and shows no message, then retry once. "
                                         "If it persists, this firmware does not accept the command.")

    async def _query(self, command: str, *, mutating: bool = False) -> str:
        frames = await self._exchange(command, until="first", mutating=mutating)
        if len(frames) != 1:
            raise CommunicationError(f"{command!r} expected one reply, got {frames}.", details={"command": command})
        self._check(command, frames)
        return frames[0]

    async def _query_int(self, command: str) -> int:
        reply = await self._query(command)
        try:
            return int(reply)
        except ValueError:
            raise CommunicationError(f"{command!r} returned {reply!r}, not a number.") from None

    async def _set(self, command: str, query: str, expected: str) -> None:
        """A setting that must be confirmed with exactly ``ST`` or ``+``, then read back."""
        frames = await self._exchange(command, mutating=True)
        self._check(command, frames)
        if frames not in (["ST"], ["+"]):
            raise CommunicationError(f"The reader did not confirm {command!r} (replied {frames}).")
        value = await self._query(query, mutating=True)
        if value.strip() != expected:
            raise CommunicationError(f"{command!r} reads back as {value!r}.", details={"command": command, "read_back": value})

    async def _timed(self, command: str, completion_s: float, *, require_busy: bool) -> None:
        """An action answered ``BY#T<ms>`` and then ``ST``."""
        frames = await self._exchange(command, completion_s=completion_s, mutating=True)
        self._check(command, frames)
        if require_busy and busy_ms(frames[0]) is None:
            raise CommunicationError(f"{command!r} was answered {frames[0]!r}, not a busy time.")
        if frames[-1] != "ST":
            raise CommunicationError(f"{command!r} ended with {frames[-1]!r}.", details={"replies": frames})

    # -- tray ---------------------------------------------------------------------------------------

    async def _move_carrier(self, where: Literal["IN", "OUT"]) -> None:
        await self._timed(f"ABSOLUTE MTP,{where}", self.tray_timeout_s, require_busy=False)
        position = await self._query("?ABSOLUTE MTP,POS", mutating=True)
        self._tray_open = {"OUT": True, "IN": False}.get(position)
        if position != where:
            raise NeedsAttention(f"The plate carrier stopped at {position!r} instead of {where!r}.", recoverable=True,
                                 suggested_action="Ask a human to check nothing blocks the carrier, then retry.")

    async def open_tray(self) -> None:
        await self._move_carrier("OUT")

    async def close_tray(self) -> None:
        await self._move_carrier("IN")

    async def tray_is_open(self) -> bool | None:
        if self._decoder is not None or self._io.locked():
            return self._tray_open
        self._tray_open = {"OUT": True, "IN": False}.get(await self._query("?ABSOLUTE MTP,POS"))
        return self._tray_open

    # -- measurements ---------------------------------------------------------------------------------

    def _plan(self, plate: Labware, wells: list[str], serpentine: bool) -> list[RowScan]:
        g = plate.require_geometry()
        (kx, ky, _), (dx, dy) = self.counts_per_mm, self.offset_mm
        rows: dict[int, list[int]] = {}
        for well in wells:
            row, column = parse_well(well)
            rows.setdefault(row, []).append(column - 1)
        scans = []
        for row in sorted(rows):
            columns = list(range(min(rows[row]), max(rows[row]) + 1))  # SCANX cannot skip wells
            if serpentine and row % 2:
                columns.reverse()
            # pyreagent's y grows toward the back from the front edge; the reader's grows toward the front from the back edge.
            y = round((g.size_y_mm - (g.a1_y_mm - row * g.pitch_y_mm) + dy) * ky)
            xs = [round((g.a1_x_mm + column * g.pitch_x_mm + dx) * kx) for column in columns]
            scans.append(RowScan(row, columns, y, xs[0], xs[-1]))
        return scans

    def _z(self, focal_height_mm: float | None) -> int:
        height = self.default_focal_height_mm if focal_height_mm is None else focal_height_mm
        return round(height * self.counts_per_mm[2])

    async def _setup(self, command: str) -> None:
        """A measurement setup command: rejections raise (``strict_setup``), a missing reply is tolerated."""
        try:
            frames = await self._exchange(command, until="first" if command[0] in "?#" else "terminal")
        except DeviceTimeout:
            log.warning("no reply to %r; continuing", command)
            return
        if command[0] in "?#":
            return  # queries the vendor software makes; the answer is not used
        if command.endswith(" CLEAR"):
            self._check(command, frames)
            if frames not in (["ST"], ["+"]):
                raise CommunicationError(f"The reader did not confirm {command!r} (replied {frames}).")
        elif command.startswith("CHECK"):
            self._check(command, [f for f in frames if f != "-"])
            if "-" in frames:
                log.warning("%r answered '-'", command)
        elif self.strict_setup:
            self._check(command, frames)

    async def _scan_row(self, scan: RowScan, direction: str, well_s: float) -> None:
        for command in (f"ABSOLUTE MTP,Y={scan.y}", f"ABSOLUTE MTP,X={scan.x_start},Y={scan.y}", f"SCAN DIRECTION={direction}"):
            self._check(command, await self._exchange(command))
        command = f"SCANX {scan.x_start},{scan.x_end},{len(scan.columns)}"
        loop = asyncio.get_running_loop()
        async with self._io:
            if self._stale:
                await self._resync()
            await self.transport.write(encode(command))
            self._stale = True
            target = self._wells_seen + len(scan.columns)
            deadline = loop.time() + self.row_timeout_s + len(scan.columns) * well_s
            done = False
            while self._wells_seen < target:
                try:
                    chunk = await self._read_chunk(min(self.reply_timeout_s + well_s, deadline - loop.time()))
                except TimeoutError:
                    raise DeviceTimeout(f"Row {row_label(scan.row)}: {len(scan.columns) - (target - self._wells_seen)} of "
                                        f"{len(scan.columns)} wells arrived.", details={"command": command}) from None
                frames = self._feed(chunk, bare_ascii=False)
                self._check(command, frames)
                done = done or "ST" in frames
            self._stale = False
            if not done:
                self._check(command, await self._collect(command))

    async def _finish(self) -> list[str]:
        """End a run (always, tolerating timeouts). Returns error replies to the step-loss checks."""
        errors = []
        for command in ("TERMINATE", *self._run_checks, "KEYLOCK OFF", "ABSOLUTE MTP,IN"):
            try:
                frames = await self._exchange(command, completion_s=self.tray_timeout_s if command.startswith("ABS") else None)
            except Exception as exc:
                log.warning("%r while ending the run: %s", command, exc)
                continue
            errors += [f"{command}: {f}" for f in frames if f.startswith("ERR")]
            if command == "ABSOLUTE MTP,IN" and frames[-1] == "ST":
                self._tray_open = False
        return errors

    async def _measure(self, plate: Labware, wells: list[str], *, decoder: Decoder, setup: list[str], checks: list[str],
                       serpentine: bool, direction: str, progress: Progress, well_s: float = 0.0) -> dict[str, float]:
        scans = self._plan(plate, wells, serpentine)
        visits = [f"{row_label(scan.row)}{column + 1}" for scan in scans for column in scan.columns]
        async with self._io:
            if self._stale:
                await self._resync()
            self._parser.reset()
            self._decoder, self._run_checks, self._wells_seen, self._wells_total = decoder, checks, 0, len(visits)
            self._progress = progress
        try:
            try:
                for command in ["KEYLOCK ON", *setup, "PREPARE REF"]:
                    await self._setup(command)
                for scan in scans:
                    await self._scan_row(scan, direction, well_s)
            except BaseException:
                await self._finish()
                self._stale = True  # scan data may still be in flight
                raise
            errors = await self._finish()
        finally:
            self._decoder, self._run_checks, self._progress = None, [], lambda f: None
        if errors:
            raise HardwareError(f"The reader reported a fault during the read: {'; '.join(errors)}", details={"replies": errors})
        if len(decoder.wells) != len(visits):
            raise CommunicationError(f"{len(decoder.wells)} well packets for {len(visits)} wells.")
        if not decoder.calibrated:
            if isinstance(decoder, AbsorbanceDecoder):
                raise CommunicationError("The reader sent no absorbance calibration packet.")
            log.warning("no calibration packet arrived; values are not dark/reference corrected")
        try:
            values = dict(zip(visits, decoder.results()))
        except CalibrationError as exc:
            raise HardwareError(f"The reader's calibration is unusable: {exc}.",
                                suggested_action="Ask a human to check the lamp and detectors.") from exc
        return {well: values[well] for well in wells}

    async def read_absorbance(self, *, plate: Labware, wavelength_nm: int, wells: list[str], progress: Progress) -> dict[str, float]:
        bandwidth = self.absorbance_bandwidth_nm or (9.0 if wavelength_nm > 315 else 5.0)
        wl, bw, n = wavelength_nm * 10, round(bandwidth * 10), self.flashes  # 0.1 nm units
        setup = ["MODE ABS", *_clears(True, False), *(f"EXCITATION {label},ABS,{wl},{bw},0" for label in (0, 1)),
                 *(f"READS {label},NUMBER={n}" for label in (0, 1)), *(f"TIME {label},READDELAY=0" for label in (0, 1)),
                 "SCAN DIRECTION=ALTUP", "#RATIO LABELS", f"BEAM DIAMETER={self.beam_diameter}", "RATIO LABELS=1"]
        return await self._measure(plate, wells, decoder=AbsorbanceDecoder(), setup=setup,
                                   checks=["CHECK MTP.STEPLOSS", "CHECK ABS.STEPLOSS"], serpentine=True, direction="ALTUP",
                                   progress=progress)

    async def read_fluorescence(
        self, *, plate: Labware, settings: FluorescenceSettings, wells: list[str], progress: Progress
    ) -> tuple[dict[str, float], int | None]:
        gain = self.default_gain if settings.gain is None else settings.gain
        n = max(1, settings.flashes or self.flashes)
        ex, em = settings.excitation_nm * 10, settings.emission_nm * 10
        exbw, embw = round(self.excitation_bandwidth_nm * 10), round(self.emission_bandwidth_nm * 10)
        z = self._z(settings.focal_height_mm)

        def label(i: int) -> list[str]:
            return [f"EXCITATION {i},FI,{ex},{exbw},0", f"EMISSION {i},FI,{em},{embw},0", f"TIME {i},INTEGRATION={self.integration_us}",
                    f"TIME {i},LAG={self.lag_us}", f"TIME {i},READDELAY=0", f"GAIN {i},VALUE={gain}", f"POSITION {i},Z={z}"]

        block = ["MODE FI.TOP", *_clears(True, True), *label(0), f"BEAM DIAMETER={FI_BEAM}",
                 "SCAN DIRECTION=UP", "RATIO LABELS=1", f"READS 0,NUMBER={n}", *label(1), f"READS 1,NUMBER={n}"]
        values = await self._measure(plate, wells, decoder=FluorescenceDecoder(), setup=block * 2,  # Tecan's software sends it twice
                                     checks=["CHECK MTP.STEPLOSS", "CHECK FI.TOP.STEPLOSS", "CHECK FI.STEPLOSS.Z"],
                                     serpentine=True, direction="UP", progress=progress)
        return values, gain

    async def read_luminescence(
        self, *, plate: Labware, integration_s: float, focal_height_mm: float | None, wells: list[str], progress: Progress
    ) -> dict[str, float]:
        n, dark_s = self.flashes, self.luminescence_dark_s
        setup = ["MODE LUM", "CHECK LUM.FIBER", "CHECK LUM.LID", "CHECK LUM.STEPLOSS", "MODE LUM", *_clears(False, True),
                 f"POSITION LUM,Z={self._z(focal_height_mm)}", f"TIME 0,INTEGRATION={round(dark_s * 1e6)}", f"READS 0,NUMBER={n}",
                 "SCAN DIRECTION=UP", "RATIO LABELS=1", "EMISSION 1,EMPTY,0,0,0", f"TIME 1,INTEGRATION={round(integration_s * 1e6)}",
                 "TIME 1,READDELAY=0", f"READS 1,NUMBER={n}", "#EMISSION ATTENUATION"]
        # Whether READS repeats both integrations is unknown; allow for it in the timeouts.
        return await self._measure(plate, wells, decoder=LuminescenceDecoder(dark_s=dark_s, integration_s=integration_s),
                                   setup=setup, checks=["CHECK MTP.STEPLOSS", "CHECK LUM.STEPLOSS"], serpentine=False,
                                   direction="UP", progress=progress, well_s=n * (dark_s + integration_s))

    async def abort_read(self) -> None:
        if self._decoder is not None:  # normally the cancelled read has already ended its run
            self._stale = True
            await self.transport.write(encode("TERMINATE"))

    # -- incubation and shaking -------------------------------------------------------------------

    async def set_temperature(self, celsius: float) -> None:
        tenths = round(celsius * 10)
        await self._set(f"TEMPERATURE PLATE,TARGET={tenths}", "?TEMPERATURE PLATE,TARGET", str(tenths))
        await self._set("TEMPERATURE PLATE,STATUS=ON", "?TEMPERATURE PLATE,STATUS", "ON")

    async def deactivate_temperature(self) -> None:
        await self._set("TEMPERATURE PLATE,STATUS=OFF", "?TEMPERATURE PLATE,STATUS", "OFF")

    async def get_temperature(self) -> tuple[float | None, float | None]:
        if self._decoder is not None or self._io.locked():
            return self._temperature  # do not interleave queries with a run or a running shake
        current = await self._query_int("?TEMPERATURE PLATE,CURRENT") / 10
        status = await self._query("?TEMPERATURE PLATE,STATUS")
        if status not in ("ON", "OFF"):
            raise CommunicationError(f"Unknown incubation status {status!r}.")
        target = await self._query_int("?TEMPERATURE PLATE,TARGET") / 10 if status == "ON" else None
        self._temperature = (current, target)
        return self._temperature

    async def shake(self, *, mode: ShakeMode, rpm: int | None, duration_s: float) -> None:
        seconds = min(999, max(1, round(duration_s)))
        await self._set(f"SHAKING MODE={SHAKE_MODES[mode]}", "?SHAKING MODE", SHAKE_MODES[mode])
        await self._set(f"SHAKING AMPLITUDE={self.shake_amplitude_um}", "?SHAKING AMPLITUDE", str(self.shake_amplitude_um))
        await self._set(f"SHAKING TIME={seconds}", "?SHAKING TIME", str(seconds))
        await self._timed("SHAKING ON", seconds + 5, require_busy=True)

    async def stop_shaking(self) -> None:
        # There is no stop command: the shake runs out and its late reply is drained before the next command.
        log.warning("the Infinite finishes a shake it has started; it cannot be stopped early")

    def info(self) -> dict[str, Any]:
        return {**super().info(), "status": self.status_text, "absorbance_beam_diameter": self.beam_diameter,
                "transport": self.transport.describe()}


__all__ = ["TecanInfiniteBackend"]
