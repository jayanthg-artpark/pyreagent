"""BMG Labtech plate readers: CLARIOstar and CLARIOstar Plus (FTDI serial link)."""

from .clariostar import ClariostarBackend

DRIVER = ClariostarBackend

__all__ = ["DRIVER", "ClariostarBackend"]
