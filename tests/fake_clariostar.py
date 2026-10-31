"""A fake BMG CLARIOstar Plus (firmware 1.35) for the driver tests: real framing and checksums.

INIT, parameter and firmware replies are the recorded frames from the protocol spec. Status replies
and data replies are built to the spec's layouts from the fake's state. Measurements take a few
status polls, then set the data-ready bit. Absorbance data are generated from known ODs with the
reader's calibration scheme; a RUN identical to a recorded one is answered with its recorded data.
"""

from __future__ import annotations

import asyncio
import math
import struct
from typing import Any

from pyreagent.devices.plate_reader.bmg_labtech.protocol import (
    LAYOUT_ABSORBANCE, LAYOUT_FLUORESCENCE, LAYOUT_LUMINESCENCE, MODE_ABSORBANCE, MODE_FLUORESCENCE, SET_PARAMETER, decode, encode,
)

H = bytes.fromhex
INIT_REPLY = H(
    "0201dc0c1325062400000100060546010302020900090d094c6001000000000d000220000e00032700000f000300000011000300000012000200001f00"
    "090100ec0000043a00212e00090104600003000000002f017845585f53503a313d6f70656e2c323d46696c74657220312c333d46696c746572203"
    "20d0a45585f4c503a313d6f70656e2c323d46696c74657220312c333d46696c74657220320d0a4253503a313d6f70656e2c323d46696c746572203"
    "12c333d46696c74657220322c343d46696c74657220330d0a454d5f53503a313d6f70656e2c323d46696c74657220312c333d46696c7465722032"
    "0d0a454d5f4c503a313d6f70656e2c323d46696c74657220312c333d46696c74657220320d0a454d5f534c49543a313d6f70656e2c323d536c69"
    "7420302e382c333d536c697420312e322c343d536c697420322e302c353d536c697420332e302c373d4650207061722c383d4650207065722c3"
    "63d4c554d206c656e73650d0a45585f534c49543a313d6f70656e2c323d536c697420302e362c333d536c697420312e302c343d536c69742031"
    "2e362c353d536c697420322e332c383d46502053746472642c373d46502055562c393d4142532c363d462d436865636b0d0a0001310007000000"
    "000000000071710d")
PARAMETERS_REPLY = H(
    "02010f0c070500240000000100000a0101010100000100ee0200000f0019040000000000000304000001000001020000000000000000000032000000"
    "000000000000000000000000000000000074006f0000000000000065000000dc050000000000000000f4010803a70408076009da08ac0d0000000000"
    "000000000000000000000000000000000000000100000001010000000000000001010000000000000012029806000000004605ee01fbff700c00000000"
    "a40058ff8e03f20460ff5511fe0b55118f1a170298065aff970668042603bc14b804080791009001463228460a0046071e000000000000000000210"
    "3d40628002c01900146001e00001411001209ac0d600900000000001e680d")
FIRMWARE_REPLY = H("0200270c0a050624000005464e6f7620323020323032300031313a35313a32310000010004f20d")

# Spec vector 9 (PLR session): absorbance, column 1 of a 96 plate, 600 nm, 5 flashes; and its data.
ABS_RUN_RECORDED = H(
    "0200900c0431e82164059604682c421d040c080080080080080080080080080000000000000000000000000000000000000000000000000000000000000"
    "00000000000000a02000000000000000000000000000000000000000000000000000000000000270f270f0501177000000064232826ca00000064000000"
    "05020000000000010000000100050001000009210d")
ABS_DATA_RECORDED = H(
    "0200cc0c020506260000a900280028001dffe20220030001000801000100000001010000000000f00036b04900368ed600367cac00368645003684ce"
    "00368d2300366f3f00365d13001595a7001598050015832000159a11001596ac0015a0620015918100157b48000a4913000a4faa000a40de000a4d8b0"
    "00a5538000a51f3000a466a000a42c600008e7c00008e7000008e2800008e2f00008e4f00008e7c00008e4500008dfc003beb7300007c940017a2d000"
    "007c31000b451f00007ae600008de600000000002d310d")
# Spec vector 12: fluorescence, column 1, 485/15 -> 528/20, gain 1000, 8.5 mm, top, 5 flashes; and its data.
FL_RUN_RECORDED = H(
    "02009c0c0431e82164059604682c421d040c080080080080080080080080080000000000000000000000000000000000000000000000000000000000000"
    "00000000000000a00000000000000000000000000000000000000000000000000000000000000270f270f05035200000100000000000c03e8133d12a71"
    "3bc1504143c000400030000000000000000000001000000010005000100000a2e0d")
FL_DATA_RECORDED = H(
    "02004a0c022506260000a1000800080003f7a0010001000801000100000001010000000000f0000000440000003e0000004800000040000000430000"
    "0045000000430000004400060d0d")
FL_RECORDED_COUNTS = [68, 62, 72, 64, 67, 69, 67, 68]
RECORDED_REPLIES = (INIT_REPLY, PARAMETERS_REPLY, FIRMWARE_REPLY, ABS_DATA_RECORDED, FL_DATA_RECORDED)

CALIBRATION_C, CALIBRATION_R = 400_000_000, 20_000


def known_od(well_index: int, wavelength_nm: float) -> float:
    """The OD the fake's absorbance data encode for a well (index in plate order) and wavelength."""
    return round(0.05 + 0.01 * well_index + wavelength_nm / 10_000, 3)


def known_rfu(well_index: int) -> int:
    return 1000 + 10 * well_index


def known_rlu(well_index: int) -> int:
    return 50 + well_index


class FakeClariostar:
    """Plays the reader: call it with a request frame, get back the reply frame."""

    def __init__(self) -> None:
        self.initialized = False
        self.busy_polls = 0
        """Status polls left before the current action finishes."""
        self.running: int | None = None
        self.drawer_open = False
        self.drawer_target: bool | None = None
        self.plate = True
        self.motors_off = False
        self.bottom_c = self.top_c = 0.0
        self.ambient_c = 24.0
        self.temperature_mode = 0
        self.pages: list[bytes] = []
        self.data_ready = False
        self.data_while_busy = False
        self.run_polls = 3
        self.page_values = 64
        """Largest number of values per absorbance data page (spectra come in several pages)."""
        self.hang = False
        """Accept RUNs but never finish them (cancel/timeout tests)."""
        self.refuse: dict[int, int] = {}
        """Command family -> quit code to refuse it with (once)."""
        self.warning: tuple[int, ...] = ()
        self.corrupt = 0
        """Corrupt the checksum of this many upcoming replies."""
        self.temperature_range = (250, 450)
        """Accepted set points, 0.1 °C (a standard incubator)."""
        self.received: list[bytes] = []
        self.runs: list[bytes] = []
        self.stopped = 0

    # -- framing --------------------------------------------------------------------------------------------

    def __call__(self, frame: bytes) -> bytes:
        payload = decode(frame)  # raises on a malformed request, failing the test
        assert payload[0] != SET_PARAMETER, "the driver sent SET_PARAMETER (0x0E)"
        self.received.append(frame)
        reply = self._handle(payload)
        out = reply if reply in RECORDED_REPLIES else encode(reply)
        if self.corrupt:
            self.corrupt -= 1
            out = out[:-2] + bytes([out[-2] ^ 0xFF]) + out[-1:]
        return out

    def _flags(self, *, refused: bool = False) -> bytes:
        b1 = 0x05 | (0x20 if self.busy_polls else 0) | (0x10 if refused else 0)
        b1 |= 0x80 if self.warning and not refused else 0
        b2 = 0x06 | (0x01 if self.data_ready else 0)
        b3 = 0x01 if self.drawer_open else 0x04 | (0x02 if self.plate else 0)
        b3 |= 0x00 if self.motors_off or not self.initialized else 0x20
        return bytes([b1, b2, b3, 0, 0])

    def _status(self, codes: tuple[int, ...] = (), *, refused: bool = False) -> bytes:
        flags = self._flags(refused=refused)
        if refused or self.warning:
            middle = bytes((codes or self.warning) + (0,) * 5)[:5]
        else:
            middle = bytes([self.running or 0, 0, 0, 0, 0]) if flags[0] & 0x20 else bytes(5)
        temps = struct.pack(">HH", round(self.bottom_c * 10), round(self.top_c * 10))
        return b"\x01" + flags + middle + temps + b"\xc0"

    # -- behaviour ------------------------------------------------------------------------------------------

    def _tick(self) -> None:
        if self.busy_polls:
            self.busy_polls -= 1
            if self.busy_polls == 0:
                self._finish()
        if self.temperature_mode > 1:
            target = self.temperature_mode / 10
            self.bottom_c = self.top_c = min(target, max(self.bottom_c, self.ambient_c) + 0.5)

    def _finish(self) -> None:
        """The current action's last busy poll has passed."""
        if self.drawer_target is not None:
            self.drawer_open, self.drawer_target = self.drawer_target, None
        if self.running == 0x04:
            if self.hang:
                self.busy_polls = 1  # never finishes
                return
            if self.pages and not self.data_ready:
                self.data_ready = True
                if self.data_while_busy:
                    self.busy_polls = 2  # data is ready while the reader still reports busy
                    return
        self.running = None

    def _handle(self, payload: bytes) -> bytes:
        family = payload[0]
        if family == 0x80:
            if not self.initialized:
                return self._status((201,), refused=True)
            self._tick()
            return self._status()
        if family in self.refuse:
            return self._status((self.refuse.pop(family),), refused=True)
        if family == 0x01:
            self.initialized, self.busy_polls = True, 2
            return INIT_REPLY
        if not self.initialized:
            return self._status((201,), refused=True)
        if family == 0x0B:
            if self.running != 0x04:
                return self._status((214,), refused=True)
            self.running, self.busy_polls, self.data_ready, self.pages, self.hang = 0x0B, 1, False, [], False
            self.stopped += 1
            return self._status()
        if self.busy_polls and family != 0x05:
            return self._status((202,), refused=True)
        if family == 0x05:
            return self._request(payload[1])
        if family == 0x03:
            self.drawer_target, self.busy_polls, self.running = bool(payload[1]), 2, 0x03
            return self._status()
        if family == 0x06:
            value = struct.unpack_from(">H", payload, 1)[0]
            if value > 1 and not self.temperature_range[0] <= value <= self.temperature_range[1]:
                return self._status((110,), refused=True)
            self.temperature_mode = value
            self.bottom_c = self.top_c = 0.0 if value == 0 else max(self.bottom_c, self.ambient_c)
            return self._status()
        if family == 0x04:
            return self._run(encode(payload), payload)
        raise AssertionError(f"unexpected command {payload.hex()}")

    def _request(self, command: int) -> bytes:
        if command == 0x07:
            return PARAMETERS_REPLY
        if command == 0x09:
            return FIRMWARE_REPLY
        if command == 0x02:
            if not self.pages:
                return self._status((213,), refused=True)
            page = self.pages.pop(0)
            if not self.pages:
                self.data_ready = False
            return page
        raise AssertionError(f"unexpected request 0x{command:02x}")

    # -- measurements -----------------------------------------------------------------------------------------

    def _run(self, frame: bytes, payload: bytes) -> bytes:
        self.runs.append(payload)
        self.running, self.busy_polls, self.data_ready = 0x04, self.run_polls, False
        if frame == ABS_RUN_RECORDED:
            self.pages = [ABS_DATA_RECORDED]
        elif frame == FL_RUN_RECORDED:
            self.pages = [FL_DATA_RECORDED]
        else:
            self.pages = self._data(payload)
        return b"\x03" + self._flags() + bytes(39)

    def _data(self, payload: bytes) -> list[bytes]:
        columns = payload[13]
        mask = payload[16:64]
        indices = [bit for bit in range(384) if mask[bit // 8] & (0x80 >> (bit % 8))]
        mode = payload[65] & 0x03
        temp = round((self.bottom_c + self.top_c) / 2 * 10)
        if mode == MODE_ABSORBANCE:
            n = payload[101]
            if n:
                wavelengths = [struct.unpack_from(">H", payload, 102 + 2 * k)[0] / 10 for k in range(n)]
            else:
                first, last, step = (v / 10 for v in struct.unpack_from(">3H", payload, 102))
                wavelengths = [first + step * k for k in range(round((last - first) / step) + 1)]
            return self._absorbance(indices, wavelengths, temp)
        layout = LAYOUT_FLUORESCENCE if mode == MODE_FLUORESCENCE else LAYOUT_LUMINESCENCE
        counts = [known_rfu(i) if mode == MODE_FLUORESCENCE else known_rlu(i) for i in indices]
        assert columns in (3, 4, 6, 8, 12, 24)
        w = len(counts)
        if layout == LAYOUT_FLUORESCENCE:
            head = struct.pack(">HHI", w, w, 260_000) + b"\x01" + struct.pack(">HHBH", 1, w, 1, 1) + bytes(9)
        else:
            head = struct.pack(">HHI", w, w, 210_000_000) + b"\x01" + struct.pack(">HH", 1, w) + bytes(12)
        return [b"\x02" + self._flags()[:5] + bytes([layout]) + head + struct.pack(">H", temp) + struct.pack(f">{w}I", *counts)]

    def _absorbance(self, indices: list[int], wavelengths: list[float], temp: int) -> list[bytes]:
        n, w = len(wavelengths), len(indices)
        groups = [[round(CALIBRATION_C * 10 ** -known_od(i, nm)) for i in indices] for nm in wavelengths]
        groups += [[7] * w, [8] * w, [CALIBRATION_R] * w]  # two undecoded groups, then the reference
        pairs = [(CALIBRATION_C, 99)] * n + [(1, 1), (1, 1), (CALIBRATION_R, 0)]
        values = [v for group in groups for v in group] + [v for pair in pairs for v in pair]
        assert len(values) == (n + 3) * (w + 2)
        pages = []
        for number, start in enumerate(range(0, len(values), self.page_values), 1):
            chunk = values[start:start + self.page_values]
            head = struct.pack(">HHI", len(chunk), len(values), 0x0002FFFD) + bytes([0x02, 0x21, (n + 2) % 256, 0, 1])
            head += struct.pack(">HBHHHB", w, 1, 1, 0, 0, number) + bytes(4)
            pages.append(b"\x02" + self._flags()[:5] + bytes([LAYOUT_ABSORBANCE]) + head + struct.pack(">H", temp)
                         + struct.pack(f">{len(chunk)}I", *chunk))
        return pages


class FakeLink:
    """Byte transport over a ``FakeClariostar``: what the driver needs from a Transport (incl. ``read_exactly``)."""

    def __init__(self, device: FakeClariostar) -> None:
        self.device = device
        self.rx = bytearray()
        self.is_open = False
        self.sent: list[bytes] = []
        self.stall_at: int | None = None
        """When a read asks for at least this many bytes, block (as if the line went slow) until cancelled."""
        self.trailing = b""
        """Bytes to append after the next reply (a stray partial frame)."""

    async def open(self) -> None:
        self.is_open = True

    async def close(self) -> None:
        self.is_open = False

    async def write(self, data: bytes) -> None:
        if not self.is_open:
            raise ConnectionError("fake link is closed")
        self.sent.append(bytes(data))
        self.rx += self.device(bytes(data)) + self.trailing
        self.trailing = b""

    async def read_exactly(self, size: int, timeout_s: float) -> bytes:
        if self.stall_at is not None and size >= self.stall_at:
            self.stall_at = None
            await asyncio.sleep(3600)
        if len(self.rx) < size:
            await asyncio.sleep(0)
            raise TimeoutError(f"{len(self.rx)} of {size} bytes")
        data = bytes(self.rx[:size])
        del self.rx[:size]
        return data

    async def discard_input(self) -> None:
        self.rx.clear()

    def describe(self) -> dict[str, Any]:
        return {"transport": "fake"}


def od_close(a: float, b: float, tolerance: float = 2e-4) -> bool:
    return math.isclose(a, b, abs_tol=tolerance)
