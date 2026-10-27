"""Agilent BioTek plate readers: Synergy H1, Cytation 1/5 and Synergy HT (FTDI USB-serial)."""

from .reader import BioTekBackend

DRIVER = BioTekBackend

__all__ = ["DRIVER", "BioTekBackend"]
