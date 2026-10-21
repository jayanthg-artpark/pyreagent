"""Protocol-level fake instruments for exercising real drivers through MockTransport."""

from __future__ import annotations


class FakeSICSBalance:
    """Answers MT-SICS level 0/1 commands like a Mettler Toledo balance."""

    def __init__(self, *, serial: str = "B021002593", balance_type: str = "XS205DU", capacity: str = "220.0000 g") -> None:
        self.serial = serial
        self.balance_type = balance_type
        self.capacity = capacity
        self.load_g = 0.0
        self.zero_g = 0.0
        self.tare_g = 0.0
        self.settled = True
        self.overloaded = False
        self.unit = "g"

    def _net(self) -> float:
        net = self.load_g - self.zero_g - self.tare_g
        return net * 1000 if self.unit == "mg" else net

    def __call__(self, data: bytes) -> bytes:
        cmd = data.decode("ascii").strip()
        if cmd == "@":
            reply = f'I4 A "{self.serial}"'
        elif cmd == "I2":
            reply = f'I2 A "{self.balance_type} {self.capacity}"'
        elif cmd in ("S", "SI"):
            if self.overloaded:
                reply = "S +"
            elif cmd == "S" and not self.settled:
                reply = "S I"
            else:
                reply = f"S {'S' if self.settled else 'D'} {self._net():>10.4f} {self.unit}"
        elif cmd == "T":
            self.tare_g = self.load_g - self.zero_g
            reply = f"T S {self.tare_g:>10.4f} g"
        elif cmd == "Z":
            self.zero_g, self.tare_g = self.load_g, 0.0
            reply = "Z A"
        else:
            reply = "ES"
        return (reply + "\r\n").encode("ascii")


class FakeNE1000:
    """Answers NE-1000 basic-mode RS-232 commands. Each DIS while running advances ``step_ml``."""

    def __init__(self, *, address: int = 0, step_ml: float = 0.25, power_on_alarm: bool = True, max_volume: float = 60.0) -> None:
        self.address = address
        self.step_ml = step_ml
        self.alarm_pending = power_on_alarm
        self.max_volume = max_volume
        self.status = "S"
        self.direction = "INF"
        self.diameter: float | None = None
        self.units: str | None = None
        self.rate: tuple[float, str] | None = None
        self.volume: float | None = None
        self.infused = 0.0
        self.withdrawn = 0.0
        self.stall_after: float | None = None
        self.commands: list[str] = []

    def _frame(self, data: str = "", status: str | None = None) -> bytes:
        return b"\x02" + f"{self.address:02d}{status or self.status}{data}".encode("ascii") + b"\x03"

    def __call__(self, raw: bytes) -> bytes:
        text = raw.decode("ascii").rstrip("\r")
        address, cmd = int(text[:2]), text[2:]
        if address != self.address:
            return b""
        if self.alarm_pending:
            self.alarm_pending = False
            return self._frame("?R", status="A")
        self.commands.append(cmd)
        name, _, arg = cmd.partition(" ")
        if name == "VER":
            return self._frame("NE1000V3.928")
        if name == "DIA":
            self.diameter = float(arg)
        elif name == "VOL":
            if arg in ("ML", "UL"):
                self.units = arg
            elif float(arg) > self.max_volume:
                return self._frame("?OOR")
            else:
                self.volume = float(arg)
        elif name == "DIR":
            self.direction = arg
        elif name == "RAT":
            value, unit = arg.split()
            self.rate = (float(value), unit)
        elif name == "CLD":
            if arg == "INF":
                self.infused = 0.0
            else:
                self.withdrawn = 0.0
        elif name == "RUN":
            self.status = "I" if self.direction == "INF" else "W"
        elif name == "STP":
            self.status = "S"
        elif name == "DIS":
            if self.status in ("I", "W") and self.volume is not None:
                moved = self.infused if self.status == "I" else self.withdrawn
                moved = min(self.volume, moved + self.step_ml)
                if self.stall_after is not None and moved >= self.stall_after:
                    self.status = "S"
                    return self._frame("?S", status="A")
                if self.status == "I":
                    self.infused = moved
                else:
                    self.withdrawn = moved
                if moved >= self.volume:
                    self.status = "S"
            return self._frame(f"I{self.infused:.3f}W{self.withdrawn:.3f}ML")
        else:
            return self._frame("?")
        return self._frame()
