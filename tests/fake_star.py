"""A fake Hamilton STAR firmware for the USB transport: real command/reply text, enough state for the driver."""

from __future__ import annotations

import re


class FakeStar:
    def __init__(self, *, tracks: int = 30, iswap: bool = True, core96: bool = True, initialised: bool = True,
                 tips_on: bool = False) -> None:
        self.tracks = tracks
        self.left_arm = 0b1 | (0b10 if iswap else 0) | (0b100 if core96 else 0)
        self.initialised = {"C0": initialised, "R0": initialised, "H0": initialised}
        self.tips = [1 if tips_on else 0] * 8
        self.fail: dict[str, str] = {}
        """Command (e.g. "C0AS" or "P1ZL") -> error text to reply with once, e.g. "er99/00 P170/00"."""
        self.liquid_levels: list[list[int]] = []
        """One C0RL reply (8 heights, 0.1 mm) per probe batch, consumed in order."""
        self.commands: list[str] = []

    def __call__(self, data: bytes) -> bytes:
        text = data.decode("ascii")
        self.commands.append(text)
        head, id_ = text[:4], re.match(r"^.{4}id(\d{4})", text).group(1)  # type: ignore[union-attr]
        prefix = f"{head}id{id_}"
        if head in self.fail:
            return (prefix + self.fail.pop(head)).encode()
        master = head.startswith("C0")
        ok = "er00/00" if master else "er00"
        data_fields = ""
        if head == "C0RM":
            data_fields = "kb11kp08"
        elif head == "C0QM":
            data_fields = (f"ka000000ke00000000xt{self.tracks:02d}xa00xw13400xl{self.left_arm:02X}xn00xr00xo00xm03500xx11400"
                           "xu3700xv3700kc0kr0ys090kl360km360ym6065yu0060yx0060")
        elif head in ("C0QW", "R0QW", "H0QW"):
            data_fields = f"qw{int(self.initialised[head[:2]])}"
        elif head in ("C0VI", "C0FI", "C0EI"):
            self.initialised[{"C0VI": "C0", "C0FI": "R0", "C0EI": "H0"}[head]] = True
        elif head == "C0RT":
            data_fields = "rt" + " ".join(str(t) for t in self.tips)
        elif head == "C0DI":
            self.tips = [0] * 8
        elif head == "C0RL":
            levels = self.liquid_levels.pop(0) if self.liquid_levels else [0] * 8
            data_fields = "lh" + " ".join(f"{h:04d}" for h in levels)
        return (prefix + ok + data_fields).encode()

    def sent(self, *prefixes: str) -> list[str]:
        """Commands sent, without their id, optionally only those starting with one of ``prefixes``."""
        stripped = [c[:4] + c[10:] for c in self.commands]
        return [c for c in stripped if not prefixes or c.startswith(prefixes)]
