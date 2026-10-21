"""qPCR (real-time PCR). Capability interface: ``Qpcr`` (extends ``Thermocycler``).
Drivers implement ``QpcrBackend``.

Brands:
    simulated/   sigmoid amplification curves with repeatable Ct per well
"""

from .analysis import estimate_ct
from .backend import QpcrBackend
from .models import QpcrProfile, QpcrResult, QpcrStage, QpcrStep
from .qpcr import Qpcr

__all__ = ["Qpcr", "QpcrBackend", "QpcrProfile", "QpcrResult", "QpcrStage", "QpcrStep", "estimate_ct"]
