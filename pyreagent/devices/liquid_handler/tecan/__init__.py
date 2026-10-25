"""Tecan liquid handlers."""

from .evo import TecanEvoBackend

DRIVER = TecanEvoBackend

__all__ = ["DRIVER", "TecanEvoBackend"]
