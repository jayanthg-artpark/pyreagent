"""Opentrons Thermocycler Module (GEN1 / GEN2) driven directly over USB serial, without a robot.

Implemented from the Opentrons firmware driver (``api/src/opentrons/drivers/thermocycler``):
115200 baud 8N1, commands are G-codes followed by `` \\r\\n``. The generation is
detected from the ``M115`` reply (GEN2 echoes the G-code):

    GEN1  replies end with "ok\\r\\nok\\r\\n", errors contain "error"
    GEN2  replies echo the G-code and end with " OK\\n", errors start "ERR" (or "async")

    M126 open lid        M127 close lid       M128 lift plate (GEN2)   M119 lid status (Lid:open)
    M104 S<c> H<s> [V<ul>] [R<°C/s>] set block   M105 block temps (T: target|none, C: current, H: hold left)
    M140 S<c> set lid    M141 lid temps       M108 lid off             M14 block off    M18 all off
    M115 device info

Values in replies are ``key:value`` pairs; ``none`` means no target. Profiles
are stepped from the host, as the Opentrons robot itself does.
"""

from __future__ import annotations

import re
from typing import Any

from ....core.errors import HardwareError, NeedsAttention, NotSupported
from ....io import SerialTransport, Transport
from ..backend import ThermocyclerBackend
from ..models import ThermocyclerStatus

_PAIR = re.compile(r"(\S+?):(\S+)")


def parse_pairs(text: str) -> dict[str, str | None]:
    return {k: (None if v.lower() == "none" else v) for k, v in _PAIR.findall(text)}


def _number(value: str | None) -> float | None:
    return None if value is None else float(value)


class OpentronsThermocyclerUSBBackend(ThermocyclerBackend):
    vendor = "Opentrons"
    model = "Thermocycler Module (USB)"

    def __init__(
        self,
        port: str | None = None,
        *,
        block_max_volume_ul: float | None = None,
        timeout_s: float = 40.0,
        poll_s: float = 1.0,
        transport: Transport | None = None,
    ) -> None:
        """
        Args:
            port: Serial port of the thermocycler, e.g. ``"COM5"`` or ``"/dev/ttyACM0"``.
            block_max_volume_ul: Sample volume per well (0-100 µL), sent with each block setpoint.
        """
        if port is None and transport is None:
            raise ValueError("port is required")
        self.transport = transport or SerialTransport(port or "", baudrate=115200)
        self.block_max_volume_ul = block_max_volume_ul
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self.hardware_generation: int | None = None
        self.device_info: dict[str, str | None] = {}

    @property
    def features(self) -> frozenset[str]:  # type: ignore[override]
        return frozenset({"ramp_rate", "plate_lift"}) if self.hardware_generation == 2 else frozenset()

    async def connect(self) -> None:
        await self.transport.open()
        first = (await self.transport.query(b"M115 \r\n", terminator=b"\n", timeout_s=self.timeout_s)).decode("ascii", "replace")
        if first.startswith("M115"):
            self.hardware_generation = 2
            info = parse_pairs(first)
            self.device_info = {"serial": info.get("SerialNo"), "model": info.get("HW"), "version": info.get("FW")}
        else:
            self.hardware_generation = 1
            await self.transport.read_until(b"ok\r\nok\r\n", self.timeout_s)
            self.device_info = dict(parse_pairs(first))

    async def disconnect(self) -> None:
        await self.transport.close()

    async def _command(self, gcode: str) -> dict[str, str | None]:
        ack = b" OK\n" if self.hardware_generation == 2 else b"ok\r\nok\r\n"
        raw = await self.transport.query(f"{gcode} \r\n".encode("ascii"), terminator=ack, timeout_s=self.timeout_s)
        text = raw.decode("ascii", "replace")
        if self.hardware_generation == 2:
            first = text.split(maxsplit=1)[0] if text.split() else ""
            if first != gcode.split()[0] and ("ERR" in text or text.startswith("async")):
                raise HardwareError(f"Thermocycler error for {gcode!r}: {text.strip()}")
        elif "error" in text.lower():
            raise HardwareError(f"Thermocycler error for {gcode!r}: {text.strip()}")
        return parse_pairs(text)

    async def open_lid(self) -> None:
        await self._command("M126")

    async def close_lid(self) -> None:
        await self._command("M127")

    async def lift_plate(self) -> None:
        if self.hardware_generation != 2:
            await super().lift_plate()
        if not await self.lid_is_open():
            raise NeedsAttention("Open the lid before lifting the plate.", recoverable=True)
        await self._command("M128")

    async def lid_is_open(self) -> bool:
        return (await self._command("M119")).get("Lid") == "open"

    async def set_block_temperature(self, target_c: float, *, ramp_rate_c_per_s: float | None = None) -> None:
        gcode = f"M104 S{round(target_c, 2)} H0"
        if self.block_max_volume_ul is not None:
            gcode += f" V{round(self.block_max_volume_ul, 2)}"
        if ramp_rate_c_per_s is not None:
            if self.hardware_generation != 2:
                raise NotSupported("Ramp rates need a Thermocycler GEN2.")
            gcode += f" R{round(ramp_rate_c_per_s, 2)}"
        await self._command(gcode)

    async def set_lid_temperature(self, target_c: float) -> None:
        await self._command(f"M140 S{round(target_c, 2)}")

    async def deactivate_block(self) -> None:
        await self._command("M14")

    async def deactivate_lid(self) -> None:
        await self._command("M108")

    async def deactivate(self) -> None:
        await self._command("M18")

    async def read(self) -> ThermocyclerStatus:
        block, lid, lid_state = await self._command("M105"), await self._command("M141"), await self._command("M119")
        return ThermocyclerStatus(
            block_c=_number(block.get("C")) or 0.0, block_target_c=_number(block.get("T")),
            lid_c=_number(lid.get("C")) or 0.0, lid_target_c=_number(lid.get("T")),
            lid_open=lid_state.get("Lid") == "open",
        )

    def info(self) -> dict[str, Any]:
        return {**super().info(), "hardware_generation": self.hardware_generation, **self.device_info,
                **self.transport.describe()}
