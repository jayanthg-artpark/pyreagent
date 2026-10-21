"""Framework pieces every device shares: backend/device split, capabilities, jobs, safety."""

from .audit import AuditLog, AuditRecord
from .backend import Backend
from .capability import CapabilityMeta, CapabilitySpec, Within, capability
from .confirmations import Confirmation, ConfirmationManager, ConfirmationStatus
from .device import Device
from .errors import (
    Cancelled,
    CommunicationError,
    ConfirmationRequired,
    DeviceBusy,
    DeviceFault,
    DeviceOffline,
    DeviceReserved,
    DeviceTimeout,
    Forbidden,
    HardwareError,
    Interrupted,
    InvalidArgument,
    NeedsAttention,
    NotFound,
    NotSupported,
    PyreagentError,
)
from .hub import DeviceHub, Invocation
from .jobs import Job, JobManager, JobStatus
from .state import DeviceState

__all__ = [
    "AuditLog", "AuditRecord", "Backend", "CapabilityMeta", "CapabilitySpec", "capability",
    "Within", "Confirmation", "ConfirmationManager", "ConfirmationStatus", "Device", "DeviceHub", "Invocation",
    "Job", "JobManager", "JobStatus", "DeviceState",
    "PyreagentError", "Cancelled", "CommunicationError", "ConfirmationRequired", "DeviceBusy", "DeviceFault",
    "DeviceOffline", "DeviceReserved", "DeviceTimeout", "Forbidden", "HardwareError", "Interrupted",
    "InvalidArgument", "NeedsAttention", "NotFound", "NotSupported",
]
