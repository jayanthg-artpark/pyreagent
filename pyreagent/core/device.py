"""Device: the capability interface (frontend) for one instrument.

Each device type (Balance, Pump, PlateReader...) subclasses ``Device`` and
declares its capabilities with ``@capability``. A ``Device`` owns a backend,
tracks its state, and enforces the mutex: while one mutating command runs,
other mutating commands fail fast with ``device_busy`` instead of queueing.
These guarantees hold for direct Python use too, not only via the server.
"""

from __future__ import annotations

import asyncio
import re
from contextvars import ContextVar
from datetime import datetime
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel

from .backend import Backend
from .capability import CapabilitySpec, capability, collect_capabilities
from .errors import (
    DeviceBusy,
    DeviceFault,
    DeviceOffline,
    InvalidArgument,
    NotFound,
    NotSupported,
    PyreagentError,
    translate_exception,
)
from .jobs import current_job, utcnow
from .state import DeviceState

B = TypeVar("B", bound=Backend)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# Devices whose mutex the current task already holds, so a capability can call
# another capability of the same device without deadlocking on itself.
_held: ContextVar[frozenset[int]] = ContextVar("pyreagent_held_devices", default=frozenset())


class Activity(BaseModel):
    capability: str
    job_id: str | None
    started_at: datetime


class Device(Generic[B]):
    device_type: ClassVar[str] = "device"

    def __init__(self, name: str, backend: B) -> None:
        """
        Args:
            name: Unique, URL-safe id on this server, e.g. ``"balance-1"``.
            backend: The driver that talks to the instrument.
        """
        if not _NAME_RE.match(name):
            raise ValueError(f"device name {name!r} must match {_NAME_RE.pattern}")
        self.name = name
        self.backend = backend
        self._state = DeviceState.OFFLINE
        self._activity: Activity | None = None
        self._activity_task: asyncio.Task[Any] | None = None
        self._last_error: dict[str, Any] | None = None

    # -- class-level capability table --------------------------------------

    @classmethod
    def capability_specs(cls) -> dict[str, CapabilitySpec]:
        specs = cls.__dict__.get("_pyreagent_specs")
        if specs is None:
            specs = collect_capabilities(cls)
            cls._pyreagent_specs = specs  # type: ignore[attr-defined]
        return specs

    def spec(self, name: str) -> CapabilitySpec:
        try:
            return self.capability_specs()[name]
        except KeyError:
            raise NotFound(
                f"Device {self.name!r} has no capability {name!r}.",
                details={"capabilities": sorted(self.capability_specs())},
            ) from None

    # -- lifecycle ----------------------------------------------------------

    @property
    def state(self) -> DeviceState:
        return self._state

    async def connect(self) -> None:
        try:
            await self.backend.connect()
        except Exception as exc:
            err = translate_exception(exc)
            self._state = DeviceState.OFFLINE
            self._last_error = {**err.to_dict(), "at": utcnow().isoformat()}
            raise DeviceOffline(f"Could not connect to {self.name}: {err.message}", details={"cause": err.to_dict()}) from exc
        self._state = DeviceState.IDLE
        self._last_error = None

    async def disconnect(self) -> None:
        if self._activity is not None:
            raise DeviceBusy(f"{self.name} is running {self._activity.capability}.", details=self._activity.model_dump(mode="json"))
        try:
            await self.backend.disconnect()
        finally:
            self._state = DeviceState.OFFLINE

    # -- execution ----------------------------------------------------------

    def check_ready(self, spec: CapabilitySpec) -> None:
        """Raise the structured error that explains why ``spec`` cannot run now."""
        meta = spec.meta
        if self._state is DeviceState.OFFLINE:
            raise DeviceOffline(f"{self.name} is offline.", details={"last_error": self._last_error})
        if self._state is DeviceState.ERROR and not (meta.read_only or meta.interrupt or meta.allow_in_error):
            raise DeviceFault(f"{self.name} is in error state and must be reset by a human.", details={"last_error": self._last_error})
        if self._activity is not None and not (meta.read_only or meta.interrupt):
            raise DeviceBusy(
                f"{self.name} is busy running {self._activity.capability}.",
                details=self._activity.model_dump(mode="json"),
            )

    def supports(self, capability_name: str) -> bool:
        """Whether this instrument can do ``capability_name``. Override when some drivers of a type can't.

        Unsupported capabilities are left out of ``describe()``, so finding devices by capability only
        finds instruments that can do it, and calls to them are refused with ``not_supported``.
        """
        return True

    def check_arguments(self, spec: CapabilitySpec, validated: BaseModel) -> None:
        """Instance-level argument checks (support, ``Within`` bounds, then ``validate_arguments``)."""
        if not self.supports(spec.name):
            raise NotSupported(f"{self.name} ({self.backend.vendor} {self.backend.model}) cannot {spec.name}.",
                               details={"capabilities": [n for n in sorted(self.capability_specs()) if self.supports(n)]})
        values = spec.values(validated)
        spec.check_bounds(values, self.limits())
        self.validate_arguments(spec.name, values)

    def validate_arguments(self, capability_name: str, values: dict[str, Any]) -> None:
        """Override for checks ``Within`` cannot express (labware exists, wells valid...).

        Must not touch the hardware: it runs before locking, and before a job is created.
        """

    async def _dispatch(self, spec: CapabilitySpec, validated: BaseModel) -> Any:
        values = spec.values(validated)
        held = _held.get()
        if id(self) in held:  # nested call from another capability of this device: trusted
            return await self._call(spec, values)
        self.check_ready(spec)
        self.check_arguments(spec, validated)
        if spec.meta.read_only or spec.meta.interrupt:
            return await self._call(spec, values)

        job = current_job()
        self._activity = Activity(capability=spec.name, job_id=job.id if job else None, started_at=utcnow())
        self._activity_task = asyncio.current_task()
        self._state = DeviceState.BUSY
        token = _held.set(held | {id(self)})
        try:
            return await self._call(spec, values)
        except asyncio.CancelledError:
            try:
                await self.on_cancel(spec.name)
            except Exception as exc:  # the instrument may now be in an unknown state
                self._fault(translate_exception(exc, mutating=True))
            raise
        finally:
            _held.reset(token)
            self._activity = None
            self._activity_task = None
            if self._state is DeviceState.BUSY:
                self._state = DeviceState.IDLE

    async def _call(self, spec: CapabilitySpec, values: dict[str, Any]) -> Any:
        try:
            return await spec.func(self, **values)
        except PyreagentError as err:
            if err.faults_device:
                self._fault(err)
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            err = translate_exception(exc, mutating=not spec.meta.read_only)
            if err.faults_device:
                self._fault(err)
            raise err from exc

    def _fault(self, err: PyreagentError) -> None:
        self._state = DeviceState.ERROR
        self._last_error = {**err.to_dict(), "at": utcnow().isoformat()}

    async def on_cancel(self, capability_name: str) -> None:
        """Called when a running job of this device is cancelled. Override to halt motion, heaters, etc."""

    async def on_reset(self) -> None:
        """Called after the backend re-initialises during ``reset``. Override to clear tracked state."""

    def cancel_activity(self) -> bool:
        """Cancel the job that holds this device, if any. For interrupt capabilities.

        The job ends as ``cancelled`` and ``on_cancel`` runs. Inline (non-job)
        calls are not cancelled. Returns True if a job was cancelled.
        """
        task = self._activity_task
        if task is None or task is asyncio.current_task() or self._activity is None or self._activity.job_id is None:
            return False
        task.cancel()
        return True

    def report_progress(self, fraction: float | None = None, message: str | None = None) -> None:
        """Update the progress of the job running this capability (no-op outside a job)."""
        job = current_job()
        if job is None:
            return
        if fraction is not None:
            job.progress = max(0.0, min(1.0, fraction))
        if message is not None:
            job.message = message

    # -- helpers for subclasses ---------------------------------------------

    @staticmethod
    def require(condition: bool, message: str, **details: Any) -> None:
        """Instance-limit check that raises a structured ``invalid_argument``."""
        if not condition:
            raise InvalidArgument(message, details=details)

    # -- description --------------------------------------------------------

    def limits(self) -> dict[str, Any]:
        """Instance limits an agent should respect (volumes, temperatures...). Override in subclasses."""
        return {}

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.device_type,
            "state": self._state.value,
            "activity": self._activity.model_dump(mode="json") if self._activity else None,
            "last_error": self._last_error,
            "driver": self.backend.info(),
            "simulated": self.backend.simulated,
            "limits": self.limits(),
            "capabilities": [
                spec.describe() for name, spec in sorted(self.capability_specs().items()) if self.supports(name)
            ],
        }

    # -- built-in capability --------------------------------------------------

    @capability(allow_in_error=True)
    async def reset(self) -> None:
        """Clear an error state by re-initialising the driver.

        Check last_error first: a fault means the instrument's physical state was
        unknown. Returns once the device is idle again; if re-initialisation fails
        the device stays in error.
        """
        await self.backend.reset()
        await self.on_reset()
        self._last_error = None  # _dispatch moves the state from BUSY back to IDLE

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r} {self._state.value} via {type(self.backend).__name__}>"
