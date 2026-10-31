"""SpectraMax serial protocol: command text, reply fields, data blocks and error codes.

Commands are ASCII lines ending in CR. A reply is one or more fields, each ended by the
prompt ``>``; field 0 is the status (``OK`` or ``FAIL<TAB><code>``) and field 1, when
there is one, the payload. How many fields a command gets is not in the reply itself,
so ``expected_fields`` holds the per-command table.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

from ....core.errors import CommunicationError, DeviceBusy, HardwareError, InvalidArgument, NeedsAttention, PyreagentError

Model = Literal["m5", "384_plus", "gemini_em"]

PROMPT = b">"

# -- field counts -----------------------------------------------------------------------------------

TWO_FIELDS = frozenset({
    "!ERROR", "!QUEUE", "!READSTAGE", "!READTYPE", "!SPEED", "!STATUS", "!TEMP", "!TRANSFER",
    "!USER_NUMBER", "!WELLSCANMODE", "!PATHCAL", "!OPTION",
})
"""Commands answered with a status field and a second field. Everything else gets one."""

PAYLOAD_COMMANDS = frozenset({"!STATUS", "!TRANSFER", "!ERROR"})
"""Commands whose second field carries data the driver cannot do without."""

_TWO_FIELD_READTYPES = frozenset({"ABSPLA", "ABSCUV", "FLUCUV", "LUMCUV"})
_GEMINI_ONE_FIELD = frozenset({"!READSTAGE", "!WELLSCANMODE"})


def expected_fields(command: str, model: Model) -> int:
    """Number of ``>``-terminated fields ``command`` is answered with on ``model``."""
    word, _, args = command.partition(" ")
    if word == "!READTYPE":
        return 2 if args.split()[:1] and args.split()[0] in _TWO_FIELD_READTYPES else 1
    if model == "gemini_em" and (word in _GEMINI_ONE_FIELD or (word == "!TEMP" and args)):
        return 1
    return 2 if word in TWO_FIELDS else 1


# -- command text -----------------------------------------------------------------------------------

def encode(command: str) -> bytes:
    return f"{command}\r".encode("ascii")


def number(value: float, decimals: int = 3) -> str:
    """Shortest fixed-point text: 5.0 -> "5", 0.30000000000000004 -> "0.3"."""
    text = f"{round(value, decimals):.{decimals}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def xpos(a1_from_left_mm: float, pitch_mm: float, columns: int) -> str:
    return f"!XPOS {a1_from_left_mm:.3f} {pitch_mm:.3f} {columns}"


def ypos(first_row_from_back_mm: float, pitch_mm: float, rows: int) -> str:
    return f"!YPOS {first_row_from_back_mm:.3f} {pitch_mm:.3f} {rows}"


def strip(first_column: int, count: int) -> str:
    return f"!STRIP {first_column} {count}"


def mode(kind: str, *args: float) -> str:
    """``mode("ENDPOINT")``, ``mode("SPECTRUM", start, step, n)``, ``mode("KINETIC", interval_s, n)``."""
    return " ".join(["!MODE", kind, *(number(a) for a in args)])


def set_temperature(celsius: float) -> str:
    return "!TEMP 0" if celsius == 0 else f"!TEMP {celsius:.1f}"


def shake_commands(before_s: float = 0, interval_s: float = 0, between_s: float = 0) -> list[str]:
    """Shaking that is part of a read: before the first read and/or between kinetic reads."""
    if not before_s and not between_s:
        return ["!SHAKE OFF"]
    between = between_s if interval_s > 0 else 0
    wait = interval_s - between if between else 0
    return ["!SHAKE ON", f"!SHAKE {number(before_s)} {number(interval_s)} {number(wait)} {number(between)} 0"]


PmtGain = Literal["auto", "high", "medium", "low"]


def pmt_commands(gain: PmtGain | int) -> list[str]:
    if gain == "auto":
        return ["!AUTOPMT ON"]
    level = str(gain) if isinstance(gain, int) else {"high": "HIGH", "medium": "MED", "low": "LOW"}[gain]
    return ["!AUTOPMT OFF", f"!PMT {level}"]


def filter_commands(cutoff_filters: list[int] | None) -> list[str]:
    """``None``: the instrument picks the emission cutoff filter itself."""
    if cutoff_filters is None:
        return ["!AUTOFILTER ON"]
    return ["!AUTOFILTER OFF", "!EMFILTER " + " ".join(str(f) for f in cutoff_filters)]


def settling_time(ms: int) -> str:
    return f"!NVRAM CARCOL {max(ms, 100)}"


# Emission cutoff filters: [min_nm, max_nm) -> filter index. Gaps (322-325, 415-420, >= 900) have none.
CUTOFF_FILTERS: tuple[tuple[int, int, int], ...] = (
    (0, 322, 1), (325, 415, 16), (420, 435, 2), (435, 455, 3), (455, 475, 4), (475, 495, 5), (495, 515, 6),
    (515, 530, 7), (530, 550, 8), (550, 570, 9), (570, 590, 10), (590, 610, 11), (610, 630, 12),
    (630, 665, 13), (665, 695, 14), (695, 900, 15),
)


def cutoff_filter(emission_nm: float) -> int | None:
    return next((index for low, high, index in CUTOFF_FILTERS if low <= emission_nm < high), None)


# -- replies ----------------------------------------------------------------------------------------

def split_fields(text: str) -> list[str]:
    """Split reply text on the prompt; trim each piece and drop empty ones."""
    return [piece.strip() for piece in text.split(">") if piece.strip()]


def is_failure(field: str) -> bool:
    return "FAIL" in field


def failure_field(fields: list[str]) -> str | None:
    """The ``FAIL`` field of a reply: field 0 if it is one, else the last field."""
    for field in (fields[:1] + fields[-1:]) if fields else []:
        if is_failure(field):
            return field
    return None


def check_reply(command: str, fields: list[str]) -> str | None:
    """Raise the pyreagent error for a failed reply. Returns a warning to log, if the reply carries one."""
    failure = failure_field(fields)
    if failure is not None:
        match = re.search(r"FAIL\s*(\d+)\s*$", failure)
        raise error_for(int(match.group(1)), command) if match else CommunicationError(
            f"{command} failed with unparsable error: {failure!r}.")
    if not any("OK" in field for field in fields):
        raise CommunicationError(f"{command} got a reply with neither OK nor FAIL: {fields!r}.")
    return fields[0] if "warning" in fields[0].lower() else None


# -- errors -----------------------------------------------------------------------------------------

Kind = Literal["host", "invalid", "busy", "attention", "link", "fault"]

ERRORS: dict[int, tuple[str, Kind]] = {
    100: ("command not found", "host"),
    101: ("invalid argument", "invalid"),
    102: ("too many arguments", "host"),
    103: ("not enough arguments", "host"),
    104: ("input line too long", "host"),
    105: ("command invalid, system busy", "busy"),
    106: ("command invalid, measurement in progress", "busy"),
    107: ("no data to transfer", "host"),
    108: ("data buffer full", "host"),
    109: ("error buffer overflow", "host"),
    110: ("stray light in the cuvette chamber; is the door open?", "attention"),
    111: ("invalid read settings", "invalid"),
    200: ("assert failed", "fault"),
    201: ("bad error number", "fault"),
    202: ("receive queue overflow", "link"),
    203: ("serial port parity error", "link"),
    204: ("serial port overrun error", "link"),
    205: ("serial port framing error", "link"),
    206: ("command generated too much output", "link"),
    207: ("fatal trap", "fault"),
    208: ("RTOS error", "fault"),
    209: ("stack overflow", "fault"),
    210: ("unknown interrupt", "fault"),
    300: ("thermistor faulty", "fault"),
    301: ("safe temperature limit exceeded", "fault"),
    302: ("low light", "attention"),
    303: ("unable to calibrate dark current", "fault"),
    304: ("signal level saturation", "attention"),
    305: ("reference level saturation", "fault"),
    306: ("plate air calibration failed, low light", "attention"),
    307: ("cuvette air reference failed", "fault"),
    308: ("stray light", "attention"),
    312: ("gain calibration failed", "fault"),
    313: ("reference gain check failed", "fault"),
    314: ("low lamp level (the lamp is ageing)", "attention"),
    315: ("cannot find zero order", "fault"),
    316: ("grating motor driver faulty", "fault"),
    317: ("monitor ADC faulty", "fault"),
    400: ("carriage motion error", "fault"),
    401: ("filter wheel error", "fault"),
    402: ("grating error", "fault"),
    403: ("stage error", "fault"),
    500: ("NVRAM CRC corrupt", "fault"),
    501: ("NVRAM grating calibration data bad", "fault"),
    502: ("NVRAM cuvette air calibration data error", "fault"),
    503: ("NVRAM plate air calibration data error", "fault"),
    504: ("NVRAM carriage offset error", "fault"),
    505: ("NVRAM stage offset error", "fault"),
}


def error_for(code: int, command: str) -> PyreagentError:
    details = {"error_code": code, "command": command}
    if code not in ERRORS:
        return HardwareError(f"{command} failed with unknown error code: {code}.", details=details)
    meaning, kind = ERRORS[code]
    message = f"SpectraMax error {code} ({meaning}) after {command}."
    if kind == "invalid":
        return InvalidArgument(message, details=details,
                               suggested_action="The reader rejected these settings; adjust the wavelengths or options and retry.")
    if kind == "busy":
        return DeviceBusy(message, details=details,
                          suggested_action="The reader is still busy (perhaps a read started elsewhere); wait and retry.")
    if kind == "attention":
        return NeedsAttention(message, recoverable=True, details=details,
                              suggested_action="Ask a human to check the plate (lid off, seated, right plate type), the doors "
                                               "and the sample signal, then retry.")
    if kind == "fault":
        action = "Ask a human to check for obstructions or a mis-seated plate, then reset." if code // 100 == 4 else \
            "Stop and ask a human to inspect or service the reader."
        return HardwareError(message, details=details, suggested_action=action)
    if kind == "link":
        return CommunicationError(message, details=details)
    return CommunicationError(message, details=details,
                              suggested_action="Check the driver's model option matches the reader; reset the device and retry.")


# -- measurement data -------------------------------------------------------------------------------

SATURATED = math.inf
_NUMBER = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
_COLUMN = re.compile(r"(\d+):(.*)", re.DOTALL)


def parse_value(text: str) -> float | None:
    """A float; ``SATURATED`` for ``#SAT``; ``None`` for anything else (``-`` marks an unread well)."""
    text = text.strip()
    if text.upper() == "#SAT":
        return SATURATED
    return float(text) if _NUMBER.fullmatch(text) else None


def _tab_fields(rest: str) -> list[str]:
    """Fields after a line's label: one separator, then TAB-separated (whitespace if there are no TABs)."""
    rest = rest[1:] if rest[:1] in ("\t", " ") else rest
    return rest.split("\t") if "\t" in rest else rest.split()


def _optional_int(text: str) -> int | None:
    value = parse_value(text)
    return round(value) if value is not None and math.isfinite(value) else None


@dataclass(frozen=True)
class DataBlock:
    """One wavelength's worth of a ``!TRANSFER`` payload."""

    time_s: float | None
    temperature_c: float | None
    wavelengths: tuple[int | None, ...]
    """From the ``L:`` line: (nm,) for absorbance, (ex, em) for fluorescence, (None, em) for luminescence."""
    columns: dict[int, list[float | None]]
    """Plate column number as labelled by the reader -> values from the first row read downwards."""

    def matrix(self) -> list[list[float | None]]:
        """Values as rows x columns, columns in label order."""
        columns = [self.columns[label] for label in sorted(self.columns)]
        rows = max((len(c) for c in columns), default=0)
        return [[c[r] if r < len(c) else None for c in columns] for r in range(rows)]


def parse_data(payload: str) -> list[DataBlock]:
    """Parse a ``!TRANSFER`` payload into its data blocks, in order.

    Line 0 is the header (time, temperature, plate name). Each block is an ``L:`` line
    followed by column lines (``3:<TAB>v<TAB>v...``). ``L:`` lines with no column lines
    after them are summaries and are dropped, as are ``exL:``/``emL:`` and other lines.
    """
    lines = [line.strip() for line in payload.splitlines() if line.strip()]
    if not lines:
        raise CommunicationError("Empty data payload from the reader.")
    header = [parse_value(part) for part in lines[0].split("\t")[:2]]
    time_s, temperature_c = (header + [None, None])[:2]
    blocks: list[tuple[tuple[int | None, ...], dict[int, list[float | None]]]] = []
    for line in lines[1:]:
        if line.startswith("L:"):
            wavelengths = tuple(_optional_int(part) for part in _tab_fields(line[2:]))
            if blocks and not blocks[-1][1]:
                blocks.pop()  # the previous L: line was a summary
            blocks.append((wavelengths, {}))
        elif (match := _COLUMN.fullmatch(line)) and blocks:
            blocks[-1][1][int(match.group(1))] = [parse_value(part) for part in _tab_fields(match.group(2))]
    return [DataBlock(time_s, temperature_c, wavelengths, columns) for wavelengths, columns in blocks if columns]
