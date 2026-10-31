"""Byonoy Absorbance 96 and Luminescence 96 readers over USB HID.

Protocol knowledge comes from studying PyLabRobot's Byonoy backend (MIT); this is an
independent implementation in pyreagent's driver model.

Every message is one 64-byte HID report: a little-endian u16 report id, 60 payload bytes,
and two routing bytes that say whether the device should answer. Replies are matched to
requests by report id only; anything else on the line is skipped. A measurement streams
back as numbered chunks (one plate row of 12 floats each), then the status report says
whether it worked.

- Absorbance 96 (PID 0x1199): fixed filters, read from the device on connect. The firmware
  needs one reference read at 660 nm after power-up; it runs on connect with the
  illumination unit on the base. Values come back as OD.
- Luminescence 96 (PID 0x119B, also the "A" variant): RLU per integration period, baseline
  subtracted, so values near 0 or slightly negative are normal for dark wells. All 96
  wells are always measured; the bit order of the firmware's well mask is unconfirmed,
  so the driver reads everything and keeps the requested wells.

Both read 96-well SBS plates only, have no tray, and report (but do not control) the
chamber temperature.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any, Literal

from ....core.errors import CommunicationError, DeviceTimeout, HardwareError, NeedsAttention
from ....io import HidTransport
from ....labware import Labware
from ..backend import PlateReaderBackend, Progress
from ..models import AbsorbanceSpec, LuminescenceSpec, ReaderSpec
from .protocol import (
    ABORT,
    ABS_RESULT,
    ABS_TRIGGER,
    DATA_READ,
    ENVIRONMENT,
    LEGACY,
    LUM_RESULT,
    LUM_TRIGGER,
    NO_REPLY,
    PRODUCT_IDS,
    REPLY,
    STATUS,
    SUPPORTED_REPORTS,
    VENDOR_ID,
    WAVELENGTHS,
    Status,
    frame,
    read_field,
    report_id,
)

log = logging.getLogger("pyreagent.byonoy")

Model = Literal["absorbance_96", "luminescence_96"]
REFERENCE_NM = 660
ALL_WELLS = b"\xff" * 12

# Absorbance 96 status error codes: (message, user can fix it?)
ABS_ERRORS: dict[int, tuple[str, bool]] = {
    1: ("The reference (calibration) read failed.", True),
    2: ("Too much ambient light reached the detector.", True),
    3: ("USB communication error inside the reader.", False),
    4: ("Hardware fault.", False),
    5: ("The reader is outside its temperature range.", True),
    6: ("No illumination unit is attached.", True),
    10: ("Internal communication fault (no acknowledgement).", False),
}
ROWS, COLUMNS = 8, 12


class ByonoyBackend(PlateReaderBackend):
    vendor = "Byonoy"

    def __init__(
        self,
        *,
        model: Model,
        serial_number: str | None = None,
        reference_read_on_connect: bool = True,
        vendor_prelude: bool = True,
        query_timeout_s: float = 10.0,
        absorbance_timeout_s: float = 120.0,
        transport: Any = None,
    ) -> None:
        """
        Args:
            model: "absorbance_96" or "luminescence_96" (also the Luminescence 96 Automate).
            serial_number: USB serial number, needed when several readers of one model are plugged in.
            reference_read_on_connect: Absorbance 96: run the firmware's 660 nm reference read on connect.
                Keep the illumination unit on the base when connecting.
            vendor_prelude: Send the two writes Byonoy's own software sends before each measurement.
            transport: Inject a transport (tests use ``MockUsbTransport``).
        """
        if model not in PRODUCT_IDS:
            raise ValueError(f"model must be one of {sorted(PRODUCT_IDS)}")
        self.reader_model = model
        self.transport = transport or HidTransport(VENDOR_ID, PRODUCT_IDS[model], serial_number=serial_number)
        self.reference_read_on_connect = reference_read_on_connect
        self.vendor_prelude = vendor_prelude
        self.query_timeout_s = query_timeout_s
        self.absorbance_timeout_s = absorbance_timeout_s
        self.wavelengths_nm: list[int] = []
        self.identity: dict[str, str] = {}
        self._measuring: int | None = None  # trigger report id while a measurement runs
        self._abort = asyncio.Event()

    @property
    def model(self) -> str:  # type: ignore[override]
        return "Absorbance 96" if self.reader_model == "absorbance_96" else "Luminescence 96"

    @property
    def spec(self) -> ReaderSpec:  # type: ignore[override]
        if self.reader_model == "absorbance_96":
            wavelengths = self.wavelengths_nm or [REFERENCE_NM]
            absorbance = AbsorbanceSpec(min_nm=min(wavelengths), max_nm=max(wavelengths), fixed_nm=sorted(wavelengths))
            return ReaderSpec(absorbance=absorbance, tray=False, plate_formats=[(ROWS, COLUMNS)])
        # Integration limits: Byonoy's presets span 0.1 s (rapid) to 20 s (ultra sensitive).
        return ReaderSpec(luminescence=LuminescenceSpec(min_integration_s=0.1, max_integration_s=20.0),
                          tray=False, plate_formats=[(ROWS, COLUMNS)])

    # -- connection ---------------------------------------------------------------------------------

    async def connect(self) -> None:
        await self.transport.open()
        try:
            self._check(await self._status())
            for index, key in ((1, "device_name"), (3, "serial_number"), (4, "firmware")):
                value = read_field(await self._query(DATA_READ, struct.pack("<HB", index, 0)))
                if isinstance(value, str):
                    self.identity[key] = value
            if self.reader_model == "absorbance_96":
                if self.reference_read_on_connect:
                    await self._measure(ABS_TRIGGER, struct.pack("<hhBB", REFERENCE_NM, 0, 1, 0), ABS_RESULT,
                                        self.absorbance_timeout_s, lambda f: None)
                reply = await self._query(WAVELENGTHS)
                self.wavelengths_nm = [nm for nm in struct.unpack_from("<30h", reply, 2) if nm > 0]
                if not self.wavelengths_nm:
                    raise HardwareError("The reader reports no absorbance wavelengths.")
        except BaseException:
            await self.transport.close()
            raise

    async def disconnect(self) -> None:
        if self._measuring is not None:
            await self.abort_read()
        await self.transport.close()

    # -- messages -----------------------------------------------------------------------------------

    async def _read_matching(self, wanted: int, deadline: float) -> bytes:
        loop = asyncio.get_running_loop()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise DeviceTimeout(f"No reply 0x{wanted:04x} from the reader.")
            try:
                data = await self.transport.read(remaining)
            except TimeoutError:
                await asyncio.sleep(0.01)  # let a cancel in before trying again
                continue
            if len(data) >= 2 and report_id(data) == wanted:
                return data

    async def _query(self, report: int, payload: bytes = b"") -> bytes:
        await self.transport.write(frame(report, payload, REPLY))
        return await self._read_matching(report, asyncio.get_running_loop().time() + self.query_timeout_s)

    async def _status(self) -> Status:
        return Status.decode(await self._query(STATUS))

    def _check(self, status: Status) -> None:
        if status.error_code == 0:
            return
        details = {"error_code": status.error_code}
        if self.reader_model == "absorbance_96" and status.error_code in ABS_ERRORS:
            message, fixable = ABS_ERRORS[status.error_code]
            if fixable:
                raise NeedsAttention(message, recoverable=True, details=details,
                                     suggested_action="Ask a human to check the illumination unit, plate and lighting, then reset.")
            raise HardwareError(message, details=details)
        raise HardwareError(f"The reader reports error 0x{status.error_code:02x}.", details=details)

    async def _measure(self, trigger: int, payload: bytes, result: int, timeout_s: float, progress: Progress) -> list[float]:
        """Trigger a measurement, collect its chunks in order, then check the status."""
        if self.vendor_prelude:  # what Byonoy's own software sends first; replies are skipped
            await self.transport.write(frame(SUPPORTED_REPORTS, b"", LEGACY))
            await self.transport.write(frame(DATA_READ, struct.pack("<HB", 7, 0), LEGACY))
        self._abort.clear()
        self._measuring = trigger
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        try:
            await self.transport.write(frame(trigger, payload, NO_REPLY if trigger == ABS_TRIGGER else LEGACY))
            chunks: dict[int, bytes] = {}
            count: int | None = None
            while count is None or len(chunks) < count:
                chunk = await self._read_matching(result, deadline)
                seq, total = chunk[2], chunk[3]
                if total == 0 or (count is not None and total != count) or seq >= total:
                    raise CommunicationError(f"Malformed result stream (chunk {seq} of {total}).")
                count = total
                chunks[seq] = chunk
                if chunk[60]:
                    log.warning("reader flagged result chunk %d (flags 0x%02x)", seq, chunk[60])
                progress(len(chunks) / count)
        except asyncio.CancelledError:
            await self.abort_read()  # the firmware keeps streaming until told to stop
            raise
        finally:
            self._measuring = None
        self._check(await self._status())
        return [value for seq in range(count) for value in struct.unpack_from("<12f", chunks[seq], 12)]

    @staticmethod
    def _pick(values: list[float], wells: list[str]) -> dict[str, float]:
        labels = [f"{'ABCDEFGH'[i // COLUMNS]}{i % COLUMNS + 1}" for i in range(ROWS * COLUMNS)]
        by_well = dict(zip(labels, values))
        return {well: round(by_well[well], 6) for well in wells}

    # -- reads --------------------------------------------------------------------------------------

    async def read_absorbance(self, *, plate: Labware, wavelength_nm: int, wells: list[str], progress: Progress) -> dict[str, float]:
        values = await self._measure(ABS_TRIGGER, struct.pack("<hhBB", wavelength_nm, 0, 0, 0), ABS_RESULT,
                                     self.absorbance_timeout_s, progress)
        return self._pick(values, wells)

    async def read_luminescence(
        self, *, plate: Labware, integration_s: float, focal_height_mm: float | None, wells: list[str], progress: Progress
    ) -> dict[str, float]:
        payload = struct.pack("<i", round(integration_s * 1_000_000)) + ALL_WELLS + b"\x00\x00"
        values = await self._measure(LUM_TRIGGER, payload, LUM_RESULT, integration_s + 60, progress)
        if len(values) % (ROWS * COLUMNS):
            raise CommunicationError(f"{len(values)} luminescence values; expected a multiple of 96.")
        return self._pick(values[: ROWS * COLUMNS], wells)  # the first plane is the result

    async def abort_read(self) -> None:
        if self._measuring is not None:
            await self.transport.write(frame(ABORT, struct.pack("<H", self._measuring), LEGACY))

    async def get_temperature(self) -> tuple[float | None, float | None]:
        reply = await self._query(ENVIRONMENT)
        return struct.unpack_from("<h", reply, 2)[0] / 100, None

    def info(self) -> dict[str, Any]:
        return {**super().info(), **self.identity, "transport": self.transport.describe()}


__all__ = ["ByonoyBackend"]
