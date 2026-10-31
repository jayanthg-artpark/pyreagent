"""Opentrons Magnetic Module GEN1/GEN2, driven through the OT-2."""

from .robot import OpentronsMagneticModuleBackend

DRIVER = OpentronsMagneticModuleBackend

__all__ = ["DRIVER", "OpentronsMagneticModuleBackend"]
