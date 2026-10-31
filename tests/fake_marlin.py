"""A fake LiquidXpert controller (Marlin G-code), answering the way the instrument's controller did."""

from __future__ import annotations

from pyreagent.io import MockTransport

# What the controller printed after reset, captured from the instrument.
BOOT_BANNER = ["start", "Marlin 2.1.2.7", "echo: Last Updated: 2026-01-22 | Author: IITM_LXP", "echo: Compiled: Aug 10 2026",
               "echo: Free Memory: 5338  PlannerBufferBytes: 1136", "echo:V88 stored settings retrieved (619 bytes; crc 59851)",
               "Testing I connection... OK", ""]
M115_REPLY = ["FIRMWARE_NAME:Marlin 2.1.2.7 (Aug 10 2026 12:14:03) SOURCE_CODE_URL:github.com/MarlinFirmware/Marlin "
              "PROTOCOL_VERSION:1.0 MACHINE_TYPE:3D Printer EXTRUDER_COUNT:0 AXIS_COUNT:4 UUID:cede2a2f-41a2-4748-9b12-c55c62f367ff",
              "Cap:EEPROM:1", "Cap:EMERGENCY_PARSER:0"]


class FakeMarlin(MockTransport):
    """Homing an axis other than Z leaves Z at its 5 mm homing clearance, as the controller does."""

    def __init__(self) -> None:
        super().__init__(self._answer)
        self.axes = {"X": 0.0, "Y": 0.0, "Z": 0.0, "A": 0.0}
        self.written: list[str] = []
        self.solenoid_on = False
        self.skew: dict[str, float] = {}
        """Axis -> mm the motor ends off target (a lost step)."""
        self.halted = False
        """Behave like an unpowered instrument: homing halts the controller."""

    async def open(self) -> None:
        await super().open()
        self._rx += self._lines(BOOT_BANNER)

    @staticmethod
    def _lines(lines: list[str]) -> bytes:
        return b"".join(line.encode() + b"\r\n" for line in lines)

    def _position(self) -> str:
        a = self.axes
        return f"X:{a['X']:.2f} Y:{a['Y']:.2f} Z:{a['Z']:.2f} A:{a['A']:.2f} Count X:0 Y:0 Z:0 A:0"

    def _answer(self, data: bytes) -> bytes:
        command = data.decode().strip()
        self.written.append(command)
        words = command.split()
        if words[0] == "G28":
            if self.halted:
                return self._lines(["Error:Printer halted. kill() called!"])
            axis = words[1][0]
            self.axes[axis] = 0.0
            self.axes["Z"] = 0.0 if axis == "Z" else 5.0
            return self._lines(["echo:busy: processing", self._position(), "ok"])
        if words[0] == "G0":
            for word in words[1:]:
                if word[0] in self.axes:
                    self.axes[word[0]] = float(word[1:]) + self.skew.get(word[0], 0.0)
            return self._lines(["ok"])
        if words[0] == "M114":
            return self._lines([self._position(), "ok"])
        if words[0] == "M115":
            return self._lines([*M115_REPLY, "ok"])
        if words[0] == "M42":
            self.solenoid_on = words[2] == "S255"
            return self._lines(["ok"])
        if words[0] in ("M400", "G4"):
            return self._lines(["ok"])
        return self._lines([f'echo:Unknown command: "{command}"', "ok"])

    def motion(self) -> list[str]:
        """Commands that move or switch something, without waits and position reads."""
        return [c for c in self.written if c not in ("M400", "M114", "M115")]
