"""A fake BioTek reader on a fake serial line: real framing for both protocol families.

``FakeBioTek`` plays the instrument (RDOP22 for H1/Cytation, legacy for the Synergy HT);
``FakeLink`` is the host's COM port. Result frames arrive asynchronously after the start
command. Output scheduled while the port is closed is held and sent when it reopens (the
HT's RTS/CTS behaviour). Error injection: ``nak``, ``fail``, ``measure_error``, ``hang``,
``corrupt_result``, ``overflow``, ``ignore_abort``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pyreagent.io import Transport

ACK, NAK, ETX, SUB = b"\x06", b"\x15", b"\x03", b"\x1a"
PARAM_COMMANDS = {"rdop22": set("z&ytgDi"), "ht": set("zngV{ir%kQt")}
NO_REPLY_PARAMS = {("i", b"1"), ("Q", b"1")}  # HT ACK-only commands


def od(row: int, column: int) -> float:
    return round(0.1 + row * 0.01 + column * 0.001, 4)


def counts(row: int, column: int) -> int:
    return 1000 * row + column


def ht_od(row: int, column: int) -> float:
    return (1000 * row + column) / 1000


def _sum(data: bytes) -> int:
    return sum(data) & 0xFF


class FakeBioTek:
    def __init__(self, model: str = "synergy_h1", *, baudrate: int = 9600, power_on: bool = True, result_delay_s: float = 0.01,
                 caps: dict[int, str] | None = None) -> None:
        self.family = "ht" if model == "synergy_ht" else "rdop22"
        self.baudrate = baudrate
        self.basecode, self.version = ("7090202", "2.24") if self.family == "ht" else ("1650200", "1.04")
        self.serial = "196804"
        self.caps: dict[int, str] = ({2000: "0", 1006: "1", 1008: "1", 1009: "1", 1012: "1", 1013: "1", 1020: "1",
                                      1031: "122,50,12,70,0000"} if self.family == "ht"
                                     else {2000: "1", 2056: "0", 2064: "1", 2138: "1"}) | (caps or {})
        self.power_on = power_on and self.family == "ht"
        self.result_delay_s = result_delay_s
        self.temperature_tenths = 236
        self.setpoint: bytes | None = None
        self.tray_open = False
        self.tray_speed: bytes | None = None
        self.geometry: bytes | None = None
        self.focus: bytes | None = None
        self.records: list[str] = []
        self.ht_setup: dict[str, bytes] = {}
        self.log: list[tuple[str, bytes]] = []
        self.aborts = 0
        # error injection
        self.nak: set[str] = set()
        self.fail: dict[str, int] = {}
        self.measure_error: int | None = None
        self.hang = False
        self.corrupt_result = False
        self.overflow: set[tuple[int, int]] = set()
        self.ignore_abort = False
        """Keep measuring after ``x`` (so a late frame can outlive the connection)."""
        # line state
        self.link: FakeLink | None = None
        self.queued = bytearray()
        """Output the reader holds until the port is opened (stale replies from an earlier session)."""
        self._pending: str | None = None
        self._timers: list[asyncio.TimerHandle] = []

    # -- output ---------------------------------------------------------------------------------------

    def _emit(self, data: bytes) -> None:
        if self.link is not None and self.link.is_open:
            self.link.push(data)
        elif self.family == "ht":
            self.queued += data

    def _later(self, data: bytes) -> None:
        self._timers.append(asyncio.get_running_loop().call_later(self.result_delay_s, self._emit, data))

    def _status(self, code: int = 0) -> bytes:
        word = f"{code:04X}".encode()
        if self.power_on:
            word = b"\x1e" + word[1:]
        return word + ETX

    # -- commands -------------------------------------------------------------------------------------

    def receive(self, data: bytes) -> None:
        if self._pending is not None:
            command, self._pending = self._pending, None
            self.log.append((command, data))
            reply = self._with_param(command, data)
            if reply is not None:
                self._emit(reply)
            return
        command = data.decode("latin-1")
        assert len(command) == 1, f"commands are one letter, got {data!r}"
        if command == "x":
            self.aborts += 1
            if self.ignore_abort:
                return
            for timer in self._timers:
                timer.cancel()
            self._timers.clear()
            return
        if command in self.nak:
            self._emit(NAK)
            return
        if command in PARAM_COMMANDS[self.family]:
            self._pending = command
            self._emit(ACK)
            return
        self.log.append((command, b""))
        self._emit(ACK + self._plain(command))

    def _plain(self, command: str) -> bytes:
        status = self._status(self.fail.get(command, 0))
        if command == "e":
            return f"{self.basecode}  Version {self.version}   ".encode() + status
        if command == "C" and self.family == "ht":
            return self.serial.ljust(8).encode() + status
        if command == "h":
            return f"{self.temperature_tenths:03d}".encode() + status
        if command in "JA":
            self.tray_open = command == "J"
            return status
        if command in "OS":
            self._start(command)
            return status
        raise AssertionError(f"unexpected command {command!r}")

    def _with_param(self, command: str, param: bytes) -> bytes | None:
        status = self._status(self.fail.get(command, 0))
        if (command, param) in NO_REPLY_PARAMS:
            self.ht_setup[command] = param
            return None
        if command == "z":
            assert len(param) == 5 and param.endswith(ETX), param
            detail = self.caps.get(int(param[:4]), "0")
            return detail.ljust(17, "0").encode() + status
        if command == "n":
            assert param == b"1"
            reply, self.power_on = status, False
            return reply
        if command == "&":
            assert param in (b"S0", b"S1")
            self.tray_speed = param
            return b"00" + status
        if command == "y":
            assert len(param) == 39 and param[:38].isdigit() and param.endswith(ETX), param
            self.geometry = param[:38]
            return status
        if command == "t":
            self.focus = param
            return status
        if command == "g":
            self.setpoint = None if param.strip(b" 0") == b"" else param
            return status
        if command == "D":
            return self._record(param, status)
        if command == "V":
            assert len(param) == 170 and param[0] == _sum(param[1:]), "bad V packet"
        if command in "V{r%kit":
            self.ht_setup[command] = param
            return status
        raise AssertionError(f"unexpected {command!r} {param!r}")

    def _record(self, param: bytes, status: bytes) -> bytes:
        text = param.removesuffix(ETX).decode()
        length, body, check = int(text[:4]), text[4:-3], int(text[-3:])
        assert length == len(body) + 3 and check == _sum(body.encode()), f"bad record {text}"
        self.records.append(body)
        if len(body) == 30:  # shake
            return status
        data = "000000000000001000000000049"
        return f"{len(data) + 8:03d}{data}{_sum(data.encode()):03d}".encode() + status

    # -- measurements ---------------------------------------------------------------------------------

    def _start(self, command: str) -> None:
        if self.hang or (command == "O" and len(self.records[-1]) == 30):
            return
        if self.measure_error is not None:
            self._later((b"\x10" if self.family == "ht" else b"") + f"{self.measure_error:04X}".encode() + ETX)
        elif command == "O":
            self._later(self._rdop22_frame(self.records[-1]))
        else:
            self._later(self._ht_frame())

    def _value(self, kind: str, row: int, column: int) -> str:
        if (row, column) in self.overflow:
            return "*******"
        return f"{od(row, column):+.4f}" if kind == "0" else f"{counts(row, column):07d}"

    def _rdop22_frame(self, body: str) -> bytes:
        r0, c0, r1, c1 = (int(body[i : i + 2]) for i in (2, 4, 6, 8))
        kind = body[33]
        rows = []
        for i, row in enumerate(range(r0, r1 + 1)):  # serpentine
            columns = range(c0, c1 + 1) if i % 2 == 0 else range(c1, c0 - 1, -1)
            rows.append(",".join(f"{row:02d},{column:02d},{self._value(kind, row, column)}" for column in columns))
        temp = f"{self.temperature_tenths:03d}"
        text = (f"000:00:00.0,{temp}," + "\r\n,".join(rows) + f"\r\n{temp}").encode() + SUB
        check = (_sum(text) + (1 if self.corrupt_result else 0)) & 0xFF
        return b"01,1,\r" + text + f"{check:03d}".encode() + SUB + self._status()

    def _ht_frame(self) -> bytes:
        r0, r1 = self.ht_setup["r"]
        c0, c1 = self.ht_setup["%"]
        lines = b""
        for row in range(r0, r1 + 1):
            fields = ["," + ("*****" if (row, c) in self.overflow else f"{round(ht_od(row, c) * 1000):+05d}") for c in range(c0, c1 + 1)]
            lines += "".join(fields).encode() + b"\r\n"
        body = b"000:00:00.0" + lines + f"{self.temperature_tenths:03d}".encode() + SUB
        check = (_sum(body) + (1 if self.corrupt_result else 0)) & 0xFF
        return b"\r" + body + bytes([check]) + SUB + self._status()


class FakeLink(Transport):
    """The host side of the serial line. Writes at the wrong baud rate are lost."""

    def __init__(self, fake: FakeBioTek, *, baudrate: int = 9600) -> None:
        super().__init__()
        self.fake = fake
        fake.link = self
        self.settings: dict[str, Any] = {"baudrate": baudrate}
        self.is_open = False
        self.sent: list[bytes] = []
        self.opened = 0
        self._rx = bytearray()
        self._arrived = asyncio.Event()

    def push(self, data: bytes) -> None:
        self._rx += data
        self._arrived.set()

    async def open(self) -> None:
        self.is_open = True
        self.opened += 1
        self._rx.clear()
        if self.fake.queued:  # held output starts flowing shortly after RTS is asserted
            asyncio.get_running_loop().call_later(0.02, self.push, bytes(self.fake.queued))
            self.fake.queued.clear()

    async def close(self) -> None:
        self.is_open = False

    async def write(self, data: bytes) -> None:
        if not self.is_open:
            raise ConnectionError("port closed")
        self.sent.append(data)
        if self.settings["baudrate"] == self.fake.baudrate:
            self.fake.receive(data)

    async def _wait(self, ready: Any, timeout_s: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while not ready():
            self._arrived.clear()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"nothing from the fake reader (pending {bytes(self._rx)!r})")
            try:
                await asyncio.wait_for(self._arrived.wait(), remaining)
            except TimeoutError:
                pass

    async def read_until(self, terminator: bytes, timeout_s: float) -> bytes:
        await self._wait(lambda: terminator in self._rx, timeout_s)
        end = self._rx.index(terminator) + len(terminator)
        data = bytes(self._rx[:end])
        del self._rx[:end]
        return data

    async def read_exactly(self, size: int, timeout_s: float) -> bytes:
        await self._wait(lambda: len(self._rx) >= size, timeout_s)
        data = bytes(self._rx[:size])
        del self._rx[:size]
        return data

    async def discard_input(self) -> None:
        self._rx.clear()

    def describe(self) -> dict[str, Any]:
        return {"transport": "fake-serial", "baudrate": self.settings["baudrate"]}
