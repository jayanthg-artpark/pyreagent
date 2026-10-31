"""Contract every plate washer driver implements.

Required: prime, stop, status. Optional operations are listed in ``features``; the frontend
refuses a call for a feature the driver does not declare before anything moves.
"""

from __future__ import annotations

import abc
from typing import Any, Callable, Literal

from ...core.backend import Backend
from ...core.errors import NotSupported
from ...labware import Labware
from .models import PlateWasherStatus, WasherProgram

Progress = Callable[[float | None, str | None], None]

FEATURES = frozenset({
    "wash",             # wash with cycles / volume / soak given in the call
    "dispense",         # dispense buffer given in the call
    "stored_programs",  # list_programs, get_program, run_program (programs saved on the washer)
    "plate_transport",  # move_plate_out / move_plate_in
})


class PlateWasherBackend(Backend):
    buffers: list[str] = []
    """Buffer bottle / inlet valve names, e.g. ["A", "B"]."""
    max_dispense_ul: float = 400.0
    plate_formats: tuple[int, ...] = (96,)
    """Well counts the manifold fits."""
    features: frozenset[str] = frozenset({"wash", "dispense"})
    """Optional operations this driver supports (subset of ``FEATURES``)."""
    prime_by: Literal["volume", "time"] = "volume"
    """Whether ``prime`` takes a buffer volume or a pumping time."""

    @abc.abstractmethod
    async def prime(self, *, buffer: str, volume_ml: float | None, duration_s: float | None) -> None:
        """Fill the lines with ``buffer``: ``volume_ml`` or ``duration_s``, as ``prime_by`` says."""

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def status(self) -> PlateWasherStatus: ...

    # -- optional (see ``features``) -------------------------------------------------------------

    def _unsupported(self, feature: str) -> NotSupported:
        return NotSupported(f"{type(self).__name__} does not support {feature}.")

    async def wash(
        self, *, plate: Labware, columns: list[int], buffer: str, cycles: int, dispense_volume_ul: float,
        soak_s: float, final_aspirate: bool, progress: Progress,
    ) -> None:
        """Each cycle: aspirate, dispense, optional soak. Then a final aspirate if asked."""
        raise self._unsupported("wash")

    async def dispense(self, *, plate: Labware, columns: list[int], buffer: str, volume_ul: float, progress: Progress) -> None:
        raise self._unsupported("dispense")

    async def list_programs(self) -> dict[int, str]:
        raise self._unsupported("stored_programs")

    async def get_program(self, number: int) -> WasherProgram:
        raise self._unsupported("stored_programs")

    async def run_program(self, number: int, progress: Progress) -> list[str]:
        """Run a stored program to the end; return the phases the washer reported."""
        raise self._unsupported("stored_programs")

    async def move_plate_out(self) -> None:
        raise self._unsupported("plate_transport")

    async def move_plate_in(self) -> None:
        raise self._unsupported("plate_transport")

    def info(self) -> dict[str, Any]:
        return {**super().info(), "buffers": list(self.buffers), "max_dispense_ul": self.max_dispense_ul,
                "plate_formats": list(self.plate_formats), "features": sorted(self.features), "prime_by": self.prime_by}
