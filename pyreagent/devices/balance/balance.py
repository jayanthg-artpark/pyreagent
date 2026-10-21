"""Balance capability interface."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ...core.capability import capability
from ...core.device import Device
from .backend import BalanceBackend
from .models import TareResult, Weight

SettleTimeout = Annotated[
    float,
    Field(gt=0, le=120, description="Seconds to wait for the reading to settle before giving up with a 'timeout' error."),
]


class Balance(Device[BalanceBackend]):
    """Laboratory balance (analytical or precision scale)."""

    device_type = "balance"

    def limits(self) -> dict[str, Any]:
        return {"capacity_g": self.backend.capacity_g}

    @capability(read_only=True)
    async def weigh(
        self,
        stable: Annotated[
            bool,
            Field(description="Wait for a settled reading (recommended). False returns the instantaneous reading, which may still be drifting."),
        ] = True,
        timeout_s: SettleTimeout = 30.0,
    ) -> Weight:
        """Read the net weight on the pan, in grams.

        Net means gross load minus the zero point and any stored tare. To weigh
        what you add to a container: place the container, call tare, add the
        material, then call weigh.
        """
        return await self.backend.read_weight(stable=stable, timeout_s=timeout_s)

    @capability
    async def tare(self, timeout_s: SettleTimeout = 30.0) -> TareResult:
        """Store the current load (e.g. an empty container) as tare.

        Waits for a settled reading first. Afterwards weigh reports only what is
        added on top of the tared load.
        """
        return TareResult(tare_g=await self.backend.tare(timeout_s=timeout_s))

    @capability
    async def zero(self, timeout_s: SettleTimeout = 30.0) -> None:
        """Set the zero point. Only call with an empty pan; it also clears any tare.

        If something is on the pan that you want to ignore, use tare instead.
        """
        await self.backend.zero(timeout_s=timeout_s)
