"""Mettler Toledo balances."""

from .sics import MettlerToledoSICSBackend

DRIVER = MettlerToledoSICSBackend

__all__ = ["DRIVER", "MettlerToledoSICSBackend"]
