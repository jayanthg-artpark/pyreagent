"""A fake Tecan EVO firmware for the USB packet transport: real framing, enough state for the driver."""

from __future__ import annotations

from typing import Any


class FakeEvo:
    def __init__(self, *, roma: bool = True, mca: bool = False, channels: int = 8, z_range: int = 2000) -> None:
        self.present = {"C5": True, "C1": roma, "W1": mca}
        self.channels = channels
        self.z_range = z_range
        self.x = {"C5": 0, "C1": 0}
        self.fail: dict[str, int] = {}
        """Mnemonic -> error code to return once, e.g. {"AGT": 25}."""
        self.commands: list[str] = []

    @staticmethod
    def reply(module: str, code: int = 0, data: Any = "") -> bytes:
        return b"\x02" + module.encode() + bytes([0x80 | code]) + str(data).encode() + b"\x00"

    def __call__(self, frame: bytes) -> bytes:
        assert frame[0] == 0x02 and frame[-1] == 0x00, frame
        body = frame[1:-1].decode("ascii")
        self.commands.append(body)
        module, command, params = body[:2], body[2:5], body[5:]
        if not self.present.get(module, False):
            return self.reply(module, 5)
        if command in self.fail:
            return self.reply(module, self.fail.pop(command))
        values = params.split(",") if params else []
        if command == "RNT":
            return self.reply(module, data=self.channels)
        if command == "RPZ":
            return self.reply(module, data=self.z_range)
        if command == "RPX":
            return self.reply(module, data=self.x.get(module, 0) if values[0] == "0" else 9000)
        if command == "RPY":
            return self.reply(module, data=3000)
        if command in ("PAA", "SAA"):
            index = 0 if command == "PAA" else 1
            if values[index]:
                self.x[module] = int(values[index])
        return self.reply(module)

    def without_queries(self) -> list[str]:
        return [c for c in self.commands if c[2:5] not in ("RPX",)]
