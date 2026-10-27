"""Byonoy plate readers: Absorbance 96 and Luminescence 96 (USB HID)."""

from .reader import ByonoyBackend

DRIVER = ByonoyBackend

__all__ = ["DRIVER", "ByonoyBackend"]
