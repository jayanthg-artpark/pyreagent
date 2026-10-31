"""A Molecular Devices SpectraMax (M5, 384 Plus or Gemini EM) speaking its RS-232 framing.

Each command is one CR-terminated line; each reply is fields ended by ``>``, with the field
counts of the protocol notes (written out here independently of the driver's table). A
read is configured by setup commands, started by ``!READ``, reports ``BUSY`` for
``busy_polls`` ``!STATUS`` calls, then ``!TRANSFER`` hands out one data block per call.
"""

from __future__ import annotations

from typing import Callable

COMMON = {"!", "!CLEAR", "!XPOS", "!YPOS", "!STRIP", "!CSPEED", "!SHAKE", "!READ", "!STATUS", "!TRANSFER", "!TEMP", "!OPEN",
          "!CLOSE", "!OPTION", "!CANCEL", "!ORDER", "!MODE", "!READTYPE", "!ERROR"}
OPTICS = {"!PMTCAL", "!AUTOPMT", "!PMT", "!FPW", "!EXWAVELENGTH", "!EMWAVELENGTH", "!AUTOFILTER", "!EMFILTER", "!READSTAGE"}
ABSORBANCE = {"!WAVELENGTH", "!CALIBRATE", "!SPEED"}
COMMANDS = {
    "m5": COMMON | OPTICS | ABSORBANCE | {"!TAG", "!NVRAM"},
    "384_plus": COMMON | ABSORBANCE,
    "gemini_em": COMMON | OPTICS | {"!TAG", "!TOPREADCLEAR", "!WELLSCANMODE"},
}
READTYPES = {"m5": {"ABSPLA", "ABSCUV", "FLU", "FLUCUV", "LUM", "LUMCUV", "POLAR", "TIME"}, "384_plus": {"PLA", "CUV"},
             "gemini_em": {"FLU", "LUM", "TIME"}}
KIND = {"ABSPLA": "abs", "PLA": "abs", "FLU": "flu", "LUM": "lum"}


def default_value(kind: str, nm: int, row: int, column: int) -> str:
    """Reading for 0-based ``row`` and 1-based plate ``column``."""
    if kind == "abs":
        return f"{0.05 + row * 0.1 + column * 0.001 + (nm - 400) / 10000:.4f}"
    return f"{(1000 if kind == 'flu' else 10) * (row + 1) + column}"


class FakeSpectraMax:
    def __init__(self, *, model: str = "m5", busy_polls: int = 2, a1_from_back_mm: float = 12.24,
                 relative_columns: bool = False) -> None:
        self.model = model
        self.busy_polls = busy_polls
        self.a1_from_back_mm = a1_from_back_mm
        self.relative_columns = relative_columns
        self.value: Callable[[str, int, int, int], str] = default_value
        self.commands: list[str] = []
        self.drawer_open = False
        self.set_point = 0.0
        self.temperature = 22.0
        self.shaking = False
        self.cancelled = False
        self.config: dict[str, str] = {}
        self.buffer: list[str] = []
        self.clock = 1000.0
        self._busy = 0
        self._line = ""
        # Fault injection, keyed by the command's first word.
        self.fail: dict[str, int] = {}             # answer FAIL <code> instead
        self.trailing_fail: dict[str, int] = {}    # answer OK, then a FAIL field
        self.extra_field: set[str] = set()         # append an unexpected field
        self.drop_second_field: set[str] = set()   # leave out the second field (keyed by the whole command)
        self.warning: str | None = None            # next OK carries this warning

    # -- framing ----------------------------------------------------------------------------------

    def _reply(self, word: str, *payload: str) -> bytes:
        status = "OK"
        if self.warning:
            status, self.warning = f"OK\t{self.warning}", None
        fields = [status, *payload]
        if self._line in self.drop_second_field:
            fields = fields[:1]
        if word in self.extra_field:
            fields.append("STRAY")
        if word in self.trailing_fail:
            return f"OK\t\r\n>FAIL\t {self.trailing_fail.pop(word)}>\r\n".encode()
        return "".join(f"{field}>\r\n" for field in fields).encode()

    def __call__(self, data: bytes) -> bytes:
        assert data.endswith(b"\r") and data.count(b"\r") == 1, data
        line = data[:-1].decode("ascii")
        self.commands.append(line)
        self._line = line
        word, _, args = line.partition(" ")
        if word in self.fail:
            return f"FAIL\t {self.fail.pop(word)}>\r\n".encode()
        if word not in COMMANDS[self.model]:
            return b"FAIL\t 100>\r\n"
        if self._busy and word not in ("!STATUS", "!CANCEL"):
            return b"FAIL\t 106>\r\n"
        return self._handle(word, args)

    def _handle(self, word: str, args: str) -> bytes:
        two = word in ("!SPEED", "!OPTION", "!ERROR") or (word == "!READSTAGE" and self.model != "gemini_em")
        if word == "!STATUS":
            busy = self._busy > 0
            self._busy = max(0, self._busy - 1)
            return self._reply(word, f"0 {'BUSY' if busy else 'IDLE'}")
        if word == "!TRANSFER":
            if not self.buffer:
                return b"OK\t\r\n>FAIL\t 107>\r\n"
            return self._reply(word, self.buffer.pop(0))
        if word == "!TEMP":
            return self._temperature(args)
        if word == "!READTYPE":
            kind = args.split()[0]
            if kind not in READTYPES[self.model]:
                return b"FAIL\t 101>\r\n"
            self.config[word] = args
            return self._reply(word, kind) if kind in ("ABSPLA", "ABSCUV", "FLUCUV", "LUMCUV") else self._reply(word)
        if word == "!OPEN" or word == "!CLOSE":
            self.drawer_open = word == "!OPEN"
        elif word == "!SHAKE" and args in ("NOW", "STOP"):
            self.shaking = args == "NOW"
        elif word == "!CLEAR" and args == "DATA":
            self.buffer.clear()
        elif word == "!CANCEL":
            self._busy, self.cancelled = 0, True
            self.buffer.clear()
        elif word == "!READ":
            self.buffer = self._measure()
            self._busy = self.busy_polls
        elif word == "!OPTION":
            return self._reply(word, f"SPECTRAMAX {self.model.upper()} V2.04")
        else:
            self.config[word] = args
        return self._reply(word, args) if two else self._reply(word)

    def _temperature(self, args: str) -> bytes:
        if args:
            value = float(args)
            if not 0 <= value <= 45:
                return b"FAIL\t 101>\r\n"
            self.set_point = value
            if self.model == "gemini_em":
                return self._reply("!TEMP")
        elif self.set_point:
            self.temperature = self.set_point  # heats instantly
        return self._reply("!TEMP", f"{self.set_point:.1f} {self.temperature:.1f}")

    # -- data -------------------------------------------------------------------------------------

    def _measure(self) -> list[str]:
        kind = KIND[self.config["!READTYPE"].split()[0]]
        x0, dx, ncol = self.config["!XPOS"].split()
        y0, dy, nrow = self.config["!YPOS"].split()
        first_col, count = (int(v) for v in self.config["!STRIP"].split())
        first_row = round((float(y0) - self.a1_from_back_mm) / float(dy))
        mode = self.config["!MODE"].split()

        def block(label: str, nm: int) -> list[str]:
            lines = [label]
            for col in range(first_col, first_col + count):
                tag = col - first_col + 1 if self.relative_columns else col
                lines.append(f"{tag}:\t" + "\t".join(self.value(kind, nm, first_row + r, col) for r in range(int(nrow))))
            return lines

        def payload(*body: str) -> str:
            self.clock += 10
            return "\r\n".join([f"{self.clock:.1f}\t{self.temperature:.1f}\t96-well", *body])

        if kind == "abs":
            if mode[0] == "SPECTRUM":
                start, step, n = (int(v) for v in mode[1:])
                return [payload(*block(f"L:\t{start + i * step}", start + i * step)) for i in range(n)]
            wavelengths = [int(w) for w in self.config["!WAVELENGTH"].split()]
            return [payload("L:\t" + "\t".join(map(str, wavelengths)), *(line for nm in wavelengths for line in block(f"L:\t{nm}", nm)))]
        em = int(self.config["!EMWAVELENGTH"])
        if kind == "flu":
            ex = int(self.config["!EXWAVELENGTH"])
            return [payload(f"exL:\t{ex}", f"emL:\t{em}", *block(f"L:\t{ex}\t{em}", em))]
        return [payload(f"emL:\t{em}", *block(f"L:\t\t{em}", em))]
