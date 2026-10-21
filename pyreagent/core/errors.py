"""Structured errors that an agent can reason about.

Every failure that leaves a device server is a ``PyreagentError``. Its payload
``{code, message, recoverable, suggested_action, details}`` tells the caller
whether a retry makes sense, whether a human must step in, or whether to abort.
"""

from __future__ import annotations

import asyncio
from typing import Any


class PyreagentError(Exception):
    """Base class. Subclasses set the class-level defaults below."""

    code: str = "internal_error"
    recoverable: bool = False
    suggested_action: str = "Stop the protocol and ask a human to check the device server logs."
    http_status: int = 500
    # True when the physical state of the instrument is now unknown, so the
    # device must go to ERROR and refuse further commands until a human resets it.
    faults_device: bool = False

    def __init__(
        self,
        message: str,
        *,
        suggested_action: str | None = None,
        recoverable: bool | None = None,
        faults_device: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if suggested_action is not None:
            self.suggested_action = suggested_action
        if recoverable is not None:
            self.recoverable = recoverable
        if faults_device is not None:
            self.faults_device = faults_device
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "recoverable": self.recoverable,
            "suggested_action": self.suggested_action,
            "details": self.details,
        }


class InvalidArgument(PyreagentError):
    code = "invalid_argument"
    recoverable = True
    suggested_action = "Fix the arguments using the limits in GET /devices/{device} and retry."
    http_status = 422


class NotFound(PyreagentError):
    code = "not_found"
    recoverable = True
    suggested_action = "Call GET /devices (or the list_devices tool) to see what exists."
    http_status = 404


class DeviceBusy(PyreagentError):
    code = "device_busy"
    recoverable = True
    suggested_action = "Wait for the running job to finish (poll GET /jobs/{job_id}), then retry."
    http_status = 409


class DeviceReserved(PyreagentError):
    code = "device_reserved"
    recoverable = True
    suggested_action = "Another caller holds a reservation on this device. Wait for it to be released or expire."
    http_status = 423


class DeviceOffline(PyreagentError):
    code = "device_offline"
    recoverable = False
    suggested_action = "Ask a human to check power and cabling, then reconnect the device."
    http_status = 503


class DeviceFault(PyreagentError):
    code = "device_fault"
    recoverable = False
    suggested_action = "Ask a human to inspect the device, then call its reset capability."
    http_status = 409


class ConfirmationRequired(PyreagentError):
    code = "confirmation_required"
    recoverable = True
    suggested_action = (
        "A human must approve this action. Show them the hazard, wait until "
        "GET /confirmations/{confirmation_id} reports 'approved', then repeat the exact "
        "same call with confirmation_id set."
    )
    http_status = 428


class Forbidden(PyreagentError):
    code = "forbidden"
    recoverable = False
    suggested_action = "This operation is reserved for a human operator."
    http_status = 403


class NeedsAttention(PyreagentError):
    """The instrument is fine but the physical setup needs a human (overload, empty reservoir, open lid)."""

    code = "needs_attention"
    recoverable = False
    suggested_action = "Ask a human to fix the physical setup described in the message, then retry."
    http_status = 409


class Interrupted(PyreagentError):
    code = "interrupted"
    recoverable = False
    suggested_action = "The action was stopped before it finished. Check details for what was done before deciding how to continue."
    http_status = 409


class CommunicationError(PyreagentError):
    code = "communication_error"
    recoverable = True
    suggested_action = "Retry once. If it fails again, ask a human to check the cable or network link."
    http_status = 502


class DeviceTimeout(PyreagentError):
    code = "timeout"
    recoverable = True
    suggested_action = "Retry once. If it times out again, ask a human to check the device."
    http_status = 504


class HardwareError(PyreagentError):
    code = "hardware_error"
    recoverable = False
    suggested_action = "Stop the protocol and ask a human to inspect the device."
    http_status = 500
    faults_device = True


class NotSupported(PyreagentError):
    code = "not_supported"
    recoverable = False
    suggested_action = "This driver cannot do that. Pick another device or capability."
    http_status = 501


class Cancelled(PyreagentError):
    code = "cancelled"
    recoverable = False
    suggested_action = "The job was cancelled. Check the device state before continuing."
    http_status = 409


def translate_exception(exc: BaseException, *, mutating: bool = False) -> PyreagentError:
    """Map an arbitrary driver exception onto a structured error.

    ``mutating`` marks exceptions raised while the instrument was being commanded
    (not just queried). A dropped link in the middle of a dispense leaves the
    physical state unknown, so the device is faulted.
    """
    if isinstance(exc, PyreagentError):
        return exc
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return DeviceTimeout(
            f"Device did not respond in time: {exc}" if str(exc) else "Device did not respond in time.",
            faults_device=mutating,
        )
    if isinstance(exc, (OSError, ConnectionError, EOFError)):
        return CommunicationError(f"Link to device failed: {type(exc).__name__}: {exc}", faults_device=mutating)
    return HardwareError(f"Unexpected driver error: {type(exc).__name__}: {exc}")
