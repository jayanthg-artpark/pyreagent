from __future__ import annotations

from enum import StrEnum


class DeviceState(StrEnum):
    """Lifecycle state every device reports.

    offline  not connected (never connected, disconnected, or connect failed)
    idle     connected and ready for a command
    busy     executing a command that holds the device mutex
    error    a fault left the physical state unknown; only reads, interrupts
             and ``reset`` are accepted until a human clears it
    """

    OFFLINE = "offline"
    IDLE = "idle"
    BUSY = "busy"
    ERROR = "error"
