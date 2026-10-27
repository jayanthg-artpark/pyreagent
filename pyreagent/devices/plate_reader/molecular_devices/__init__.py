"""Molecular Devices SpectraMax readers: M5 / M5e, 384 Plus and Gemini EM (RS-232)."""

from .spectramax import SpectraMaxBackend

DRIVER = SpectraMaxBackend

__all__ = ["DRIVER", "SpectraMaxBackend"]
