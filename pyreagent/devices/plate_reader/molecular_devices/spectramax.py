"""Molecular Devices SpectraMax M5 / M5e, 384 Plus and Gemini EM readers over RS-232.

Protocol knowledge comes from studying PyLabRobot's SpectraMax backends (MIT); this is
an independent implementation in pyreagent's driver model. That source had no captured
traffic from a real instrument, so the reply framing below is what its hand-written test
fixtures show. **Not yet run on a real reader.**

Serial 9600 8N1, no flow control. Commands are ASCII lines ending in CR; replies are fields
each ended by the prompt ``>``. The connect handshake is a bare ``!``. A measurement is a
setup sequence, ``!READ``, ``!STATUS`` polled until it says ``IDLE``, then ``!TRANSFER``
(once per spectrum step). Values come back as OD, RFU or RLU; nothing is converted.

Supported, by ``model``:

- ``m5``: absorbance and absorbance spectra, fluorescence (top/bottom), luminescence.
- ``384_plus``: absorbance and absorbance spectra.
- ``gemini_em``: fluorescence (top/bottom), luminescence (top).
- All: drawer open/close, heating 25..45 °C (no cooling; ``!TEMP 0`` turns it off), and an
  immediate linear shake (``!SHAKE NOW`` .. ``!SHAKE STOP``) with no speed setting.

Limitations and assumptions:

- Wavelength ranges are vendor figures, not from the protocol, and unverified: M5
  absorbance 200-1000 nm, fluorescence 250-850 nm; 384 Plus absorbance 190-1000 nm; Gemini
  EM excitation 250-850 nm, emission 360-850 nm. The reader rejects anything it cannot do
  (error 101/111 -> ``invalid_argument``). The shake pattern is assumed linear.
- Plates are described by geometry (A1 centre and pitch from the plate's left and back
  edges), so plates need ``geometry``. M5 and 384 Plus read the whole plate unless
  ``region_reads`` is on; the Gemini EM reads the smallest rectangle holding the requested
  wells. Only the requested wells are returned.
- Saturated (``#SAT``) and unread (``-``) wells are left out of the result, with a logged
  warning: result values are plain floats that must survive JSON. A spectrum leaves out a
  well if any of its points is saturated or unread.
- No read height command exists; focal height is not offered. Luminescence integration
  time is not settable, so the spec offers a single nominal 1 s (the reader's own timing
  applies). Fluorescence gain is a driver option (``pmt_gain``), not a per-read argument.
- Reply field counts follow a per-command table (``protocol.expected_fields``). A missing
  trailing field that carries no data is tolerated after ``extra_field_timeout_s``; a
  ``FAIL`` that follows the expected fields is caught by a short linger; unread bytes are
  drained before every command, so a wrong count cannot shift later replies.
- ``abort_read`` sends ``!CANCEL``, whose effect is unverified.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from dataclasses import dataclass
from typing import Any, Literal

from ....core.errors import CommunicationError, DeviceTimeout, InvalidArgument, PyreagentError
from ....io import Transport, line_transport
from ....labware import Labware, parse_well, row_label
from ..backend import FluorescenceSettings, PlateReaderBackend, Progress
from ..models import AbsorbanceSpec, FluorescenceSpec, LuminescenceSpec, ReaderSpec, ShakeMode, ShakeSpec
from .protocol import (
    PAYLOAD_COMMANDS,
    PROMPT,
    DataBlock,
    Model,
    PmtGain,
    check_reply,
    cutoff_filter,
    encode,
    expected_fields,
    filter_commands,
    is_failure,
    mode as read_mode,
    parse_data,
    pmt_commands,
    set_temperature,
    settling_time,
    shake_commands,
    split_fields,
    strip,
    xpos,
    ypos,
)

log = logging.getLogger("pyreagent.spectramax")

Setting = Literal["on", "once", "off"]


@dataclass(frozen=True)
class ModelInfo:
    name: str
    absorbance_nm: tuple[int, int] | None
    fluorescence_nm: tuple[int, int, int, int] | None  # excitation min/max, emission min/max
    luminescence: bool


MODELS: dict[str, ModelInfo] = {
    "m5": ModelInfo("SpectraMax M5", (200, 1000), (250, 850, 250, 850), True),
    "384_plus": ModelInfo("SpectraMax 384 Plus", (190, 1000), None, False),
    "gemini_em": ModelInfo("SpectraMax Gemini EM", None, (250, 850, 360, 850), True),
}
MAX_SET_POINT_C = 45.0
NOMINAL_INTEGRATION_S = 1.0
GEMINI_FLASHES = 6


@dataclass(frozen=True)
class Region:
    """The rectangle of wells a read covers: 0-based first row, 1-based first column."""

    first_row: int
    rows: int
    first_column: int
    columns: int


class SpectraMaxBackend(PlateReaderBackend):
    vendor = "Molecular Devices"

    def __init__(
        self,
        port: str | None = None,
        *,
        model: Model = "m5",
        host: str | None = None,
        tcp_port: int | None = None,
        baudrate: int = 9600,
        transport: Transport | None = None,
        region_reads: bool | None = None,
        emission_cutoff: Literal["auto", "table"] | None = None,
        pmt_gain: PmtGain = "auto",
        pmt_calibration: Setting | None = None,
        absorbance_calibration: Setting = "once",
        carriage_speed: Literal["normal", "slow"] = "normal",
        settling_time_ms: int | None = 100,
        luminescence_emission_nm: int = 0,
        min_set_point_c: float = 25.0,
        poll_interval_s: float = 1.0,
        read_timeout_s: float = 600.0,
        command_timeout_s: float = 60.0,
        extra_field_timeout_s: float = 0.5,
        linger_s: float = 0.05,
    ) -> None:
        """
        Args:
            port: Serial port (usually a USB-serial adapter); or ``host``/``tcp_port`` for a serial server.
            model: "m5" (also M5e), "384_plus" or "gemini_em". The protocol cannot tell them apart.
            region_reads: Read only the rectangle of requested wells. Default: on for the Gemini EM
                (the known path), off for M5/384 Plus, where it is unverified.
            emission_cutoff: "auto" lets the reader pick the emission cutoff filter; "table" picks it from
                the emission wavelength. Default: "auto" on the M5, "table" on the Gemini EM.
            pmt_gain: Fluorescence/luminescence PMT gain: "auto", "high", "medium" or "low".
            pmt_calibration: PMT calibration per read. Default: "once" on the M5, "on" on the Gemini EM.
            absorbance_calibration: Absorbance air calibration per read.
            settling_time_ms: M5 carriage settling time (at least 100), sent before every read as
                ``!NVRAM CARCOL``. None sends nothing (it writes NVRAM on every read).
            luminescence_emission_nm: Emission wavelength for luminescence; 0 means all wavelengths
                (the Gemini EM default; unverified on the M5).
            min_set_point_c: Lowest offered set point; the reader only heats, so it cannot go below ambient.
            extra_field_timeout_s: How long to wait for a reply field that carries no needed data.
            linger_s: How long to listen after a complete reply for a trailing FAIL field.
        """
        if model not in MODELS:
            raise ValueError(f"model must be one of {sorted(MODELS)}")
        if not 0 < min_set_point_c <= MAX_SET_POINT_C:
            raise ValueError(f"min_set_point_c must be within 0 .. {MAX_SET_POINT_C:g}")
        self.reader_model: Model = model
        self.hardware = MODELS[model]
        gemini = model == "gemini_em"
        self.transport = transport or line_transport(port=port, host=host, tcp_port=tcp_port, baudrate=baudrate)
        self.region_reads = gemini if region_reads is None else region_reads
        self.emission_cutoff = emission_cutoff or ("table" if gemini else "auto")
        self.pmt_gain = pmt_gain
        self.pmt_calibration = pmt_calibration or ("on" if gemini else "once")
        self.absorbance_calibration = absorbance_calibration
        self.carriage_speed = carriage_speed
        self.settling_time_ms = settling_time_ms
        self.luminescence_emission_nm = luminescence_emission_nm
        self.min_set_point_c = min_set_point_c
        self.poll_interval_s = poll_interval_s
        self.read_timeout_s = read_timeout_s
        self.command_timeout_s = command_timeout_s
        self.extra_field_timeout_s = extra_field_timeout_s
        self.linger_s = linger_s
        self.firmware: list[str] = []
        self._lock = asyncio.Lock()
        self._reading = False
        self._tray_open: bool | None = None
        self._temperature: tuple[float | None, float | None] = (None, None)

    @property
    def model(self) -> str:  # type: ignore[override]
        return self.hardware.name

    @property
    def spec(self) -> ReaderSpec:  # type: ignore[override]
        info = self.hardware
        absorbance = fluorescence = luminescence = None
        if info.absorbance_nm:
            low, high = info.absorbance_nm
            absorbance = AbsorbanceSpec(min_nm=low, max_nm=high, spectrum=True, max_spectrum_points=high - low + 1)
        if info.fluorescence_nm:
            ex_low, ex_high, em_low, em_high = info.fluorescence_nm
            fluorescence = FluorescenceSpec(min_excitation_nm=ex_low, max_excitation_nm=ex_high, min_emission_nm=em_low,
                                            max_emission_nm=em_high, optics=["top", "bottom"])
        if info.luminescence:
            luminescence = LuminescenceSpec(min_integration_s=NOMINAL_INTEGRATION_S, max_integration_s=NOMINAL_INTEGRATION_S)
        return ReaderSpec(absorbance=absorbance, fluorescence=fluorescence, luminescence=luminescence,
                          temperature_c=(self.min_set_point_c, MAX_SET_POINT_C), shaking=ShakeSpec(modes=["linear"]),
                          tray=True, needs_geometry=True)

    # -- connection ---------------------------------------------------------------------------------

    async def connect(self) -> None:
        await self.transport.open()
        try:
            await self._send("!")
            with contextlib.suppress(PyreagentError):  # firmware text; its layout is unknown
                fields = await self._send("!OPTION")
                self.firmware = fields[1].split() if len(fields) > 1 else []
        except BaseException:
            await self.transport.close()
            raise

    async def disconnect(self) -> None:
        await self.transport.close()

    async def reset(self) -> None:
        await self.disconnect()
        await self.connect()
        await self._send("!CLEAR ERROR")
        self._reading = False

    # -- messages -----------------------------------------------------------------------------------

    async def _read_fields(self, timeout_s: float) -> list[str]:
        return split_fields((await self.transport.read_until(PROMPT, timeout_s)).decode("ascii", "replace"))

    async def _exchange(self, command: str) -> list[str]:
        """Send ``command`` and collect its reply fields (without judging them)."""
        expected = expected_fields(command, self.reader_model)
        later_timeout = self.command_timeout_s if command.split()[0] in PAYLOAD_COMMANDS else self.extra_field_timeout_s
        async with self._lock:
            await self.transport.discard_input()
            await self.transport.write(encode(command))
            fields: list[str] = []
            while len(fields) < expected:
                wait = later_timeout if fields else self.command_timeout_s
                try:
                    fields += await self._read_fields(wait)
                except TimeoutError:
                    if not fields:
                        raise DeviceTimeout(f"No reply to {command} within {wait:g}s.",
                                            suggested_action="Check the reader is on and the serial cable and port.") from None
                    log.debug("%s: %d of %d reply fields", command, len(fields), expected)
                    return fields
                if any(is_failure(f) for f in fields):
                    return fields
            with contextlib.suppress(TimeoutError):  # a FAIL can follow the fields we expected
                while True:
                    fields += await self._read_fields(self.linger_s)
        return fields

    async def _send(self, command: str) -> list[str]:
        fields = await self._exchange(command)
        if warning := check_reply(command, fields):
            log.warning("%s: %s", command, warning)
        return fields

    async def _payload(self, command: str) -> str:
        fields = await self._send(command)
        if len(fields) < 2:
            raise CommunicationError(f"{command} returned no data: {fields!r}.")
        return fields[1]

    # -- plate description ------------------------------------------------------------------------

    def _region(self, plate: Labware, wells: list[str]) -> Region:
        if not self.region_reads:
            return Region(0, plate.rows, 1, plate.columns)
        positions = [parse_well(well) for well in wells]
        rows = [row for row, _ in positions]
        columns = [column for _, column in positions]
        return Region(min(rows), max(rows) - min(rows) + 1, min(columns), max(columns) - min(columns) + 1)

    @staticmethod
    def _position(plate: Labware, region: Region) -> list[str]:
        """XPOS/YPOS/STRIP: pyreagent geometry is front-left based; the reader measures from the left and back edges."""
        g = plate.require_geometry()
        from_back = g.size_y_mm - g.a1_y_mm + region.first_row * g.pitch_y_mm
        return [xpos(g.a1_x_mm, g.pitch_x_mm, plate.columns), ypos(from_back, g.pitch_y_mm, region.rows),
                strip(region.first_column, region.columns)]

    def _by_well(self, block: DataBlock, region: Region) -> dict[str, float | None]:
        labels = set(block.columns)
        wanted = range(region.first_column, region.first_column + region.columns)
        if set(wanted) <= labels:
            offset = 0  # columns labelled with plate column numbers
        elif labels == set(range(1, region.columns + 1)):
            offset = region.first_column - 1  # labelled from the first column read
        else:
            raise CommunicationError(f"Data has columns {sorted(labels)}; expected {list(wanted)}.")
        values: dict[str, float | None] = {}
        for label, column in block.columns.items():
            if label + offset in wanted:
                for index, value in enumerate(column[: region.rows]):
                    values[f"{row_label(region.first_row + index)}{label + offset}"] = value
        return values

    @staticmethod
    def _keep(values: dict[str, float | None], wells: list[str]) -> dict[str, float]:
        kept = {well: v for well in wells if (v := values.get(well)) is not None and math.isfinite(v)}
        saturated = [well for well in wells if values.get(well) == math.inf]
        unread = [well for well in wells if well not in kept and well not in saturated]
        if saturated:
            log.warning("saturated wells left out of the result: %s", ", ".join(saturated))
        if unread:
            log.warning("unread wells left out of the result: %s", ", ".join(unread))
        return kept

    # -- reads --------------------------------------------------------------------------------------

    def _cspeed(self) -> str:
        return f"!CSPEED {8 if self.carriage_speed == 'normal' else 1}"

    def _nvram(self) -> list[str]:
        return [settling_time(self.settling_time_ms)] if self.settling_time_ms is not None else []

    def _pmtcal(self) -> str:
        return f"!PMTCAL {self.pmt_calibration.upper()}"

    async def _measure(self, setup: list[str], transfers: int, progress: Progress) -> list[list[DataBlock]]:
        """Run the setup, start the read, wait for IDLE, then fetch ``transfers`` data blocks."""
        progress(0.0)
        for command in setup:
            await self._send(command)
        await self._send("!READ")
        self._reading = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.read_timeout_s
        try:
            while not await self._idle():
                if loop.time() > deadline:
                    raise DeviceTimeout(f"The read did not finish within {self.read_timeout_s:g}s.",
                                        suggested_action="Raise read_timeout_s for long reads, or ask a human to check the reader.")
                await asyncio.sleep(self.poll_interval_s)
        except Exception:
            self._reading = False  # on cancellation it stays set, so abort_read knows to send !CANCEL
            raise
        self._reading = False
        results = []
        for index in range(transfers):
            results.append(parse_data(await self._payload("!TRANSFER")))
            progress((index + 1) / transfers)
        return results

    async def _idle(self) -> bool:
        tokens = (await self._payload("!STATUS")).split()
        if len(tokens) < 2:
            raise CommunicationError(f"Unexpected status {tokens!r}.")
        return tokens[1] == "IDLE"

    @staticmethod
    def _one(blocks: list[DataBlock]) -> DataBlock:
        if len(blocks) != 1:
            raise CommunicationError(f"Expected one data block, got {len(blocks)}.")
        return blocks[0]

    def _absorbance_setup(self, plate: Labware, region: Region, first_nm: int, measurement: str) -> list[str]:
        model_specific = ["!TAG OFF", *self._nvram(), "!READTYPE ABSPLA"] if self.reader_model == "m5" else ["!READTYPE PLA"]
        return ["!CLEAR DATA", *self._position(plate, region), self._cspeed(), *shake_commands(), f"!WAVELENGTH {first_nm}",
                f"!CALIBRATE {self.absorbance_calibration.upper()}", measurement, "!ORDER COLUMN", "!SPEED OFF", *model_specific]

    async def read_absorbance(self, *, plate: Labware, wavelength_nm: int, wells: list[str], progress: Progress) -> dict[str, float]:
        region = self._region(plate, wells)
        (blocks,) = await self._measure(self._absorbance_setup(plate, region, wavelength_nm, read_mode("ENDPOINT")), 1, progress)
        block = self._one(blocks)
        if block.wavelengths[:1] != (wavelength_nm,):
            raise CommunicationError(f"Asked for {wavelength_nm} nm; the data is for {block.wavelengths}.")
        return self._keep(self._by_well(block, region), wells)

    async def read_absorbance_spectrum(
        self, *, plate: Labware, wavelengths_nm: list[int], wells: list[str], progress: Progress
    ) -> dict[str, list[float]]:
        region = self._region(plate, wells)
        start, count = wavelengths_nm[0], len(wavelengths_nm)
        step = wavelengths_nm[1] - start if count > 1 else 1
        setup = self._absorbance_setup(plate, region, start, read_mode("SPECTRUM", start, step, count))
        per_step: list[dict[str, float | None]] = []
        for nm, blocks in zip(wavelengths_nm, await self._measure(setup, count, progress)):
            block = self._one(blocks)
            if block.wavelengths[:1] != (nm,):
                raise CommunicationError(f"Spectrum step for {nm} nm came back as {block.wavelengths}.")
            per_step.append(self._by_well(block, region))
        spectra = {well: [step_values.get(well) for step_values in per_step] for well in wells}
        complete = {well: points for well, points in spectra.items() if all(p is not None and math.isfinite(p) for p in points)}
        if dropped := [well for well in wells if well not in complete]:
            log.warning("wells with saturated or unread points left out of the spectrum: %s", ", ".join(dropped))
        return complete  # type: ignore[return-value]

    def _cutoff(self, emission_nm: int) -> list[str]:
        if self.emission_cutoff == "auto":
            return filter_commands(None)
        index = cutoff_filter(emission_nm)
        if index is None:
            raise InvalidArgument(f"No emission cutoff filter covers {emission_nm} nm (gaps at 322-325 and 415-420 nm).",
                                  suggested_action="Shift emission_nm by a few nm.")
        return filter_commands([index])

    async def read_fluorescence(
        self, *, plate: Labware, settings: FluorescenceSettings, wells: list[str], progress: Progress
    ) -> tuple[dict[str, float], int | None]:
        region = self._region(plate, wells)
        gemini = self.reader_model == "gemini_em"
        flashes = settings.flashes or (GEMINI_FLASHES if gemini else 10)
        stage = ["!TOPREADCLEAR ON"] if gemini else []
        stage.append(f"!READSTAGE {'TOP' if settings.optics == 'top' else 'BOT'}")
        tail = ["!TAG OFF", "!WELLSCANMODE OFF"] if gemini else ["!TAG OFF", *self._nvram()]
        setup = ["!CLEAR DATA", *self._position(plate, region), self._cspeed(), *shake_commands(), f"!FPW {flashes}",
                 *pmt_commands(self.pmt_gain), f"!EXWAVELENGTH {settings.excitation_nm}", f"!EMWAVELENGTH {settings.emission_nm}",
                 *self._cutoff(settings.emission_nm), *stage, self._pmtcal(), read_mode("ENDPOINT"), "!ORDER COLUMN", *tail,
                 "!READTYPE FLU"]
        (blocks,) = await self._measure(setup, 1, progress)
        return self._keep(self._by_well(self._one(blocks), region), wells), None

    async def read_luminescence(
        self, *, plate: Labware, integration_s: float, focal_height_mm: float | None, wells: list[str], progress: Progress
    ) -> dict[str, float]:
        region = self._region(plate, wells)
        emission = f"!EMWAVELENGTH {self.luminescence_emission_nm}"
        if self.reader_model == "gemini_em":
            setup = ["!CLEAR DATA", "!TAG OFF", "!WELLSCANMODE OFF", *self._position(plate, region), *shake_commands(),
                     "!READTYPE LUM", emission, f"!FPW {GEMINI_FLASHES}", "!TOPREADCLEAR OFF", "!READSTAGE TOP",
                     *pmt_commands(self.pmt_gain), self._cspeed(), self._pmtcal(), read_mode("ENDPOINT"), "!ORDER COLUMN"]
        else:
            setup = ["!CLEAR DATA", "!READSTAGE TOP", *self._position(plate, region), self._cspeed(), *shake_commands(),
                     *pmt_commands(self.pmt_gain), emission, self._pmtcal(), read_mode("ENDPOINT"), "!ORDER COLUMN", "!TAG OFF",
                     *self._nvram(), "!READTYPE LUM"]
        (blocks,) = await self._measure(setup, 1, progress)
        return self._keep(self._by_well(self._one(blocks), region), wells)

    async def abort_read(self) -> None:
        if not self._reading:
            return
        self._reading = False
        try:
            await self._send("!CANCEL")
        except PyreagentError as exc:
            log.warning("!CANCEL failed: %s", exc)

    # -- drawer, temperature, shaking ---------------------------------------------------------------

    async def open_tray(self) -> None:
        await self._send("!OPEN")
        self._tray_open = True

    async def close_tray(self) -> None:
        await self._send("!CLOSE")
        self._tray_open = False

    async def tray_is_open(self) -> bool | None:
        """As last commanded; the protocol has no drawer query, so None until the first open/close."""
        return self._tray_open

    async def set_temperature(self, celsius: float) -> None:
        await self._send(set_temperature(celsius))

    async def deactivate_temperature(self) -> None:
        await self._send(set_temperature(0))

    async def get_temperature(self) -> tuple[float | None, float | None]:
        if self._reading:  # the reader refuses commands while measuring
            return self._temperature
        fields = await self._send("!TEMP")
        text = fields[1] if len(fields) > 1 else fields[0].replace("OK", "")
        try:
            set_point, current = (float(token) for token in text.split()[:2])
        except ValueError:
            raise CommunicationError(f"Unexpected temperature reply {fields!r}.") from None
        self._temperature = (current, set_point if set_point > 0 else None)
        return self._temperature

    async def shake(self, *, mode: ShakeMode, rpm: int | None, duration_s: float) -> None:
        await self._send("!SHAKE NOW")
        await asyncio.sleep(duration_s)
        await self._send("!SHAKE STOP")

    async def stop_shaking(self) -> None:
        await self._send("!SHAKE STOP")

    def info(self) -> dict[str, Any]:
        return {**super().info(), "firmware": " ".join(self.firmware) or None, **self.transport.describe()}


__all__ = ["SpectraMaxBackend"]
