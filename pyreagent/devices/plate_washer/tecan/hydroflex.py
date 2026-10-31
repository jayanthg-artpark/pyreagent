"""Tecan HydroFlex plate washer over USB.

Protocol (Tecan ASCII framing, ``vendors/tecan/text.py``), as confirmed on a HydroFlex:

    ?DEVICEID                     -> "HydroFlex"
    INIT FORCE                    -> BY#T<ms>, ST        homes the plate transport and the manifold
    ?CONFIG OFFSET OUTPOS         -> "-960"              the transport's load position
    STEPABS MAIN TRANSPORT <pos>  -> BY#T<ms>, ST        (HOME moves it back in)
    PRIME CHA=<1-4>,TIME=<ms>     -> BY#T<ms>, ST
    #PROGRAM / #PLATE             -> "1:ABE|6:DISP"      stored programs / plate definitions
    ?PROGRAM <n>,PLATE.NR | STEPS | STEP=<i>
    PROGRAM <n>,0,START           -> MSG<k>: <PHASE> and BY#T<ms> frames ..., then ST
    ?VERSION, ?RINSE STATUS
    errors                        -> ERR<n>:<text>, e.g. ERR25:Remote Mode Not Allowed - Switch Instrument Off/On

Every busy frame says how long the washer will be busy; that sets how long to wait for the next
frame. The washer ignores commands it does not know, so an unanswered command times out.

Washing is done with programs stored on the washer (made on its touch screen or in Tecan's
software): pyreagent lists, reads and runs them. Volumes, cycles and soak times cannot be set
per call, so ``wash`` and ``dispense`` are not offered. Buffers are the four supply channels.
There is no known abort command: ``stop`` cannot interrupt a running program.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from typing import Any, Mapping

from ....core.errors import CommunicationError, DeviceTimeout, HardwareError, NeedsAttention, PyreagentError
from ....io import UsbBulkTransport
from ....vendors.tecan.text import FrameError, busy_ms, encode, take_frame
from ..backend import PlateWasherBackend, Progress
from ..models import PlateWasherStatus, WasherProgram

log = logging.getLogger("pyreagent.tecan.hydroflex")

VENDOR_ID = 0x0C47
PRODUCT_ID = 0x8002
DEVICE_ID = "HydroFlex"
QUERY_TIMEOUT_S = 5.0
BUSY_MARGIN_S = 5.0

_ERROR = re.compile(r"ERR(\d+):?(.*)")


def error_for(command: str, reply: str) -> PyreagentError:
    match = _ERROR.fullmatch(reply)
    code = int(match.group(1)) if match else None
    text = (match.group(2) if match else reply).strip()
    message = f"HydroFlex {command!r} failed: {reply}"
    details = {"command": command, "code": code, "reply": reply}
    if code == 25:
        return NeedsAttention(message, suggested_action="Ask a human to switch the washer off and on, then reset the device.",
                              details=details)
    if any(word in text.lower() for word in ("waste", "bottle", "liquid", "empty", "clog")):
        return NeedsAttention(message, suggested_action="Ask a human to check the bottles and the waste, then retry.", details=details)
    return HardwareError(message, details=details)


def parse_numbered(reply: str) -> dict[int, str]:
    """``1:ABE|6:DISP`` -> {1: "ABE", 6: "DISP"}."""
    entries: dict[int, str] = {}
    if not reply.strip():
        return entries
    for entry in reply.split("|"):
        number, sep, name = entry.partition(":")
        if not sep or not number.strip().isdigit():
            raise CommunicationError(f"Cannot read list entry {entry!r} in HydroFlex reply {reply!r}.")
        entries[int(number)] = name
    return entries


class TecanHydroFlexBackend(PlateWasherBackend):
    vendor = "Tecan"
    model = "HydroFlex"
    features = frozenset({"stored_programs", "plate_transport"})
    prime_by = "time"

    def __init__(
        self,
        *,
        serial_number: str | None = None,
        channels: Mapping[str, int] | None = None,
        plate_formats: tuple[int, ...] = (96, 384),
        transport: Any = None,
    ) -> None:
        """
        Args:
            channels: buffer name -> supply channel 1-4, e.g. ``{"wash": 1, "water": 2}``. Default: "1".."4".
            plate_formats: well counts the installed manifold handles.
        """
        self.channels = dict(channels or {str(n): n for n in range(1, 5)})
        if not set(self.channels.values()) <= {1, 2, 3, 4}:
            raise ValueError("HydroFlex supply channels are 1 to 4")
        self.buffers = list(self.channels)
        self.plate_formats = tuple(plate_formats)
        self.transport = transport or UsbBulkTransport(VENDOR_ID, PRODUCT_ID, serial_number=serial_number, read_size=64, timeout_s=1.0)
        self._buffer = bytearray()
        self._lock = asyncio.Lock()
        self.firmware: str | None = None

    # -- framing ----------------------------------------------------------------------------------

    async def _next_frame(self, command: str, timeout_s: float) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            try:
                frame = take_frame(self._buffer)
            except FrameError as err:
                raise CommunicationError(f"Garbled reply from the HydroFlex to {command!r}: {err}.") from err
            if frame is not None:
                log.debug("hydroflex << %s", frame)
                if frame.startswith("ERR"):
                    raise error_for(command, frame)
                return frame
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise DeviceTimeout(f"No reply from the HydroFlex to {command!r} within {timeout_s:g} s.",
                                    details={"command": command})
            try:
                self._buffer += await self.transport.read(min(remaining, 1.0))
            except TimeoutError:
                continue

    async def _write(self, command: str) -> None:
        self._buffer.clear()  # anything left over belongs to an earlier, finished or abandoned command
        log.debug("hydroflex >> %s", command)
        await self.transport.write(encode(command))

    async def _query(self, command: str) -> str:
        async with self._lock:
            await self._write(command)
            return await self._next_frame(command, QUERY_TIMEOUT_S)

    async def _action(self, command: str) -> None:
        """An action: a timed busy frame, then ST."""
        async with self._lock:
            await self._write(command)
            busy = await self._next_frame(command, QUERY_TIMEOUT_S)
            ms = busy_ms(busy)
            if ms is None:
                raise CommunicationError(f"HydroFlex answered {command!r} with {busy!r} instead of starting.")
            done = await self._next_frame(command, ms / 1000 + BUSY_MARGIN_S)
            if done != "ST":
                raise CommunicationError(f"HydroFlex finished {command!r} with {done!r} instead of ST.")

    # -- connection -------------------------------------------------------------------------------

    async def connect(self) -> None:
        await self.transport.open()
        try:
            device = await self._query("?DEVICEID")
            if device != DEVICE_ID:
                raise NeedsAttention(f"The USB device answered {device!r}, not {DEVICE_ID!r}.",
                                     suggested_action="Check the serial_number points at the HydroFlex.")
            self.firmware = await self._query("?VERSION")
            await self._action("INIT FORCE")  # homes the plate transport and the manifold
        except BaseException:
            await self.transport.close()
            raise

    async def disconnect(self) -> None:
        await self.transport.close()

    # -- operations -------------------------------------------------------------------------------

    async def prime(self, *, buffer: str, volume_ml: float | None, duration_s: float | None) -> None:
        assert duration_s is not None and math.isfinite(duration_s)  # the frontend requires it (prime_by = "time")
        await self._action(f"PRIME CHA={self.channels[buffer]},TIME={round(duration_s * 1000)}")

    async def list_programs(self) -> dict[int, str]:
        return parse_numbered(await self._query("#PROGRAM"))

    async def list_plates(self) -> dict[int, str]:
        return parse_numbered(await self._query("#PLATE"))

    async def get_program(self, number: int) -> WasherProgram:
        name = (await self.list_programs())[number]
        plate = await self._query(f"?PROGRAM {number},PLATE.NR")
        count = await self._query(f"?PROGRAM {number},STEPS")
        if not plate.isdigit() or not count.isdigit():
            raise CommunicationError(f"HydroFlex program {number} reports plate {plate!r} and {count!r} steps.")
        steps = [await self._query(f"?PROGRAM {number},STEP={i}") for i in range(1, int(count) + 1)]
        return WasherProgram(number=number, name=name, plate=int(plate), steps=steps)

    async def run_program(self, number: int, progress: Progress) -> list[str]:
        command = f"PROGRAM {number},0,START"
        phases: list[str] = []
        async with self._lock:
            await self._write(command)
            timeout, started = QUERY_TIMEOUT_S, False
            while True:
                frame = await self._next_frame(command, timeout)
                if frame == "ST":
                    if not started:
                        raise CommunicationError(f"HydroFlex answered {command!r} with ST without starting.")
                    return phases
                ms = busy_ms(frame)
                if ms is not None:
                    started, timeout = True, ms / 1000 + BUSY_MARGIN_S
                elif frame.startswith("MSG"):
                    phases.append(frame)
                    progress(None, frame.partition(":")[2].strip() or frame)
                else:
                    raise CommunicationError(f"HydroFlex sent {frame!r} while running {command!r}.")

    async def move_plate_out(self) -> None:
        position = await self._query("?CONFIG OFFSET OUTPOS")
        if re.fullmatch(r"-?\d+", position) is None:
            raise CommunicationError(f"HydroFlex reported {position!r} as the plate out position.")
        await self._action(f"STEPABS MAIN TRANSPORT {position}")

    async def move_plate_in(self) -> None:
        await self._action("STEPABS MAIN TRANSPORT HOME")

    async def stop(self) -> None:
        # No abort command is known; the frontend stops waiting, the washer finishes what it is doing.
        log.warning("Tecan HydroFlex: stop requested; a running program or move finishes on the washer.")

    async def status(self) -> PlateWasherStatus:
        # The washer answers one command at a time: while it runs something, report that instead of asking.
        notes = {"state": "busy"} if self._lock.locked() else {"state": "idle", "rinse": await self._query("?RINSE STATUS")}
        if self.firmware:
            notes["firmware"] = self.firmware
        return PlateWasherStatus(buffers_ml={b: None for b in self.buffers}, waste_ml=None, waste_capacity_ml=None,
                                 waste_full=None, notes=notes)

    def info(self) -> dict[str, Any]:
        return {**super().info(), "channels": self.channels, "firmware": self.firmware, **self.transport.describe()}
