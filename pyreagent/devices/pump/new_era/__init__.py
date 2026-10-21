"""New Era Pump Systems."""

from .ne1000 import NewEraSyringePumpBackend

DRIVER = NewEraSyringePumpBackend

__all__ = ["DRIVER", "NewEraSyringePumpBackend"]
