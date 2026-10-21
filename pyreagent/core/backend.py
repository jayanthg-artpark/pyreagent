"""Backend (driver) base class.

A backend speaks one vendor protocol to one instrument over a transport
(serial, USB, CAN, TCP, HTTP, an ESP32 bridge...). It knows nothing about
HTTP, jobs, agents or locking; those live in the layers above it.
"""

from __future__ import annotations

import abc
from typing import Any, ClassVar


class Backend(abc.ABC):
    vendor: ClassVar[str] = "unknown"
    model: ClassVar[str] = "unknown"
    simulated: ClassVar[bool] = False

    @abc.abstractmethod
    async def connect(self) -> None:
        """Open the link and bring the instrument to a known, ready state."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Leave the instrument safe and close the link."""

    async def reset(self) -> None:
        """Recover from a fault. Default: reconnect."""
        await self.disconnect()
        await self.connect()

    def info(self) -> dict[str, Any]:
        """Static facts about the driver and instrument, shown in GET /devices."""
        cls = type(self)
        return {
            "vendor": self.vendor,
            "model": self.model,
            "simulated": self.simulated,
            "driver": f"{cls.__module__}.{cls.__qualname__}",
        }
