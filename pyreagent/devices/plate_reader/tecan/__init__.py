"""Tecan plate readers: Infinite 200 PRO, M series (USB bulk)."""

from .infinite import TecanInfiniteBackend

DRIVER = TecanInfiniteBackend

__all__ = ["DRIVER", "TecanInfiniteBackend"]
