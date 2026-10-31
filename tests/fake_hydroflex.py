"""A fake Tecan HydroFlex: real framing, scripted replies modelled on frames captured from a washer."""

from __future__ import annotations

from pyreagent.vendors.tecan.text import encode, take_frame

PROGRAM_6 = ["MSG1: INIT", "BY#T25000", "MSG8: PRIME", "BY#T30000", "MSG4: DISPENSE", "BY#T100000", "BY#T100000",
             "MSG2: ASPIRATE", "BY#T61000", "ST"]


class FakeHydroFlex:
    def __init__(self) -> None:
        self.programs = {1: "ABE", 6: "DISP", 20: "WASH"}
        self.out_position = "-960"
        self.fail: dict[str, str] = {}
        """Command prefix -> ERR reply sent instead of the normal one, once."""
        self.split = False
        """Deliver each reply frame in two USB reads."""
        self.commands: list[str] = []

    def __call__(self, data: bytes) -> list[bytes]:
        buffer = bytearray(data)
        command = take_frame(buffer)
        assert command is not None and not buffer, data
        self.commands.append(command)
        for prefix, error in list(self.fail.items()):
            if command.startswith(prefix):
                del self.fail[prefix]
                return self._frames([error])
        return self._frames(self._reply(command))

    def _reply(self, command: str) -> list[str]:
        if command == "?DEVICEID":
            return ["HydroFlex"]
        if command == "?VERSION":
            return ["V_4.20_11/17_Hydro_RX"]
        if command == "?RINSE STATUS":
            return ["NO RINSE STARTED"]
        if command == "?CONFIG OFFSET OUTPOS":
            return [self.out_position]
        if command == "#PROGRAM":
            return ["|".join(f"{n}:{name}" for n, name in self.programs.items())]
        if command == "#PLATE":
            return ["1:GRE96ft|6:COS96fb"]
        if command == "?PROGRAM 6,PLATE.NR":
            return ["1"]
        if command == "?PROGRAM 6,STEPS":
            return ["2"]
        if command == "?PROGRAM 6,STEP=1":
            return ["CYCLE|NUM=1"]
        if command == "?PROGRAM 6,STEP=2":
            return ["DISPENSE|ZPOSITION=OVERFLOW|ZPOSITIONCUSTOM=0|VOLUME=50|CHANNEL=1|FLOWRATE=300"]
        if command == "PROGRAM 6,0,START":
            return PROGRAM_6
        if command in ("INIT FORCE", "STEPABS MAIN TRANSPORT HOME") or command.startswith(("STEPABS MAIN TRANSPORT ", "PRIME ")):
            return ["BY#T25000", "ST"]
        return []  # the washer ignores commands it does not know

    def _frames(self, texts: list[str]) -> list[bytes]:
        frames = [encode(t) for t in texts]
        if self.split:
            return [part for f in frames for part in (f[:5], f[5:])]
        return frames
