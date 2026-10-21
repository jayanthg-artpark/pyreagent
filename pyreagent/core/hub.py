"""DeviceHub: the runtime a device server wraps.

The HTTP routes and the MCP tools are thin adapters over ``DeviceHub.invoke``,
so every path into a device gets the same validation, reservation check,
job handling, idempotency and audit record.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Literal

from pydantic import BaseModel

from .audit import AuditLog
from .capability import CapabilitySpec
from .device import Device
from .errors import (
    DeviceReserved,
    InvalidArgument,
    NotFound,
    PyreagentError,
    translate_exception,
)
from .jobs import Job, JobManager, JobStatus, utcnow

log = logging.getLogger("pyreagent")

ANONYMOUS = "anonymous"


class Invocation(BaseModel):
    status: Literal["completed", "accepted"]
    """completed: ``result`` holds the return value. accepted: poll ``job``."""
    device: str
    capability: str
    result: Any = None
    job: Job | None = None


@dataclass
class Reservation:
    holder: str
    expires_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {"holder": self.holder, "expires_at": self.expires_at.isoformat()}


class DeviceHub:
    def __init__(
        self,
        devices: Iterable[Device[Any]],
        *,
        name: str = "pyreagent",
        audit: AuditLog | None = None,
        job_retention: int = 500,
        idempotency_ttl_s: float = 24 * 3600,
    ) -> None:
        self.name = name
        self.devices: dict[str, Device[Any]] = {}
        for device in devices:
            if device.name in self.devices:
                raise ValueError(f"duplicate device name {device.name!r}")
            self.devices[device.name] = device
        self.audit = audit or AuditLog()
        self.jobs = JobManager(retention=job_retention)
        self._reservations: dict[str, Reservation] = {}
        self._idempotency: dict[str, tuple[str, float, asyncio.Task[Invocation]]] = {}
        self._idempotency_ttl = idempotency_ttl_s

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Connect every device. A device that fails to connect stays offline; the server still starts."""
        for device in self.devices.values():
            await self.connect_device(device.name, caller="server")

    async def stop(self) -> None:
        await self.jobs.shutdown()
        for device in self.devices.values():
            try:
                await device.disconnect()
            except Exception:
                log.exception("error disconnecting %s", device.name)

    async def connect_device(self, name: str, *, caller: str) -> dict[str, Any]:
        device = self.device(name)
        try:
            await device.connect()
            self.audit.record(event="connect", caller=caller, device=name, outcome="ok")
        except PyreagentError as err:
            log.warning("device %s offline: %s", name, err.message)
            self.audit.record(event="connect", caller=caller, device=name, outcome="failed", error=err.to_dict())
        return self.describe_device(name)

    async def disconnect_device(self, name: str, *, caller: str) -> dict[str, Any]:
        await self.device(name).disconnect()
        self.audit.record(event="disconnect", caller=caller, device=name, outcome="ok")
        return self.describe_device(name)

    # -- discovery ----------------------------------------------------------

    def device(self, name: str) -> Device[Any]:
        try:
            return self.devices[name]
        except KeyError:
            raise NotFound(f"No device named {name!r} on server {self.name!r}.", details={"devices": sorted(self.devices)}) from None

    def describe_device(self, name: str) -> dict[str, Any]:
        device = self.device(name)
        reservation = self._reservation(name)
        return {
            **device.describe(),
            "reservation": reservation.to_dict() if reservation else None,
            "running_jobs": [j.id for j in self.jobs.running_for(name)],
        }

    def describe(self) -> dict[str, Any]:
        return {"server": self.name, "devices": [self.describe_device(n) for n in self.devices]}

    # -- invocation -----------------------------------------------------------

    async def invoke(
        self,
        device_name: str,
        capability_name: str,
        args: dict[str, Any] | None = None,
        *,
        caller: str = ANONYMOUS,
        idempotency_key: str | None = None,
    ) -> Invocation:
        """Run one capability call through every safety check.

        Short capabilities return ``completed`` with the result. Long-running
        ones return ``accepted`` with a job to poll. Failures raise ``PyreagentError``.
        """
        canonical: dict[str, Any] | None = None
        try:
            device = self.device(device_name)
            spec = device.spec(capability_name)
            validated = spec.validate(args)
            canonical = validated.model_dump(mode="json")
            fingerprint = None
            if idempotency_key:
                fingerprint = self._fingerprint(device_name, capability_name, canonical, caller)
                replay = self._idempotent_replay(idempotency_key, fingerprint)
                if replay is not None:
                    return await asyncio.shield(replay)
            self._check_reservation(device, spec, caller)
            device.check_ready(spec)
            device.check_arguments(spec, validated)
        except PyreagentError as err:
            self.audit.record(
                event="invoke", caller=caller, device=device_name, capability=capability_name,
                args=canonical if canonical is not None else args, outcome="rejected", error=err.to_dict(),
            )
            raise

        task = asyncio.Task(
            self._execute(device, spec, validated, canonical, caller),
            loop=asyncio.get_running_loop(),
            eager_start=True,
        )
        if idempotency_key and fingerprint:
            self._idempotency[idempotency_key] = (fingerprint, time.monotonic(), task)
        # Shielded: a dropped HTTP connection must not abort a physical operation halfway.
        return await asyncio.shield(task)

    async def _execute(
        self,
        device: Device[Any],
        spec: CapabilitySpec,
        validated: BaseModel,
        canonical: dict[str, Any],
        caller: str,
    ) -> Invocation:
        common: dict[str, Any] = dict(event="invoke", caller=caller, device=device.name, capability=spec.name, args=canonical)
        if spec.meta.long_running:
            async def run() -> Any:
                return spec.dump_result(await device._dispatch(spec, validated))

            job = self.jobs.submit(
                device=device.name, capability=spec.name, args=canonical, caller=caller,
                run=run, on_finished=self._job_finished,
            )
            if job.status is JobStatus.FAILED and job._exception is not None:
                # Failed before its first await, e.g. the device was claimed by someone else.
                self.audit.record(**common, outcome="rejected", error=job.error, job_id=job.id)
                raise job._exception
            self.audit.record(**common, outcome="accepted", job_id=job.id)
            return Invocation(status="accepted", device=device.name, capability=spec.name, job=job)

        started = time.perf_counter()
        try:
            result = spec.dump_result(await device._dispatch(spec, validated))
        except Exception as exc:
            err = translate_exception(exc)
            self.audit.record(**common, outcome="failed", error=err.to_dict(), duration_ms=_ms(started))
            if err is exc:
                raise
            raise err from exc
        self.audit.record(**common, outcome="completed", result=result, duration_ms=_ms(started))
        return Invocation(status="completed", device=device.name, capability=spec.name, result=result)

    def _job_finished(self, job: Job) -> None:
        duration = (job.finished_at - job.started_at).total_seconds() * 1000 if job.started_at and job.finished_at else None
        self.audit.record(
            event="job_finished", caller=job.caller, device=job.device, capability=job.capability, args=job.args,
            outcome=job.status.value, result=job.result, error=job.error, job_id=job.id, duration_ms=duration,
        )

    # -- reservations ------------------------------------------------------------

    def _reservation(self, device_name: str) -> Reservation | None:
        r = self._reservations.get(device_name)
        if r is not None and r.expires_at <= utcnow():
            del self._reservations[device_name]
            return None
        return r

    def _check_reservation(self, device: Device[Any], spec: CapabilitySpec, caller: str) -> None:
        if spec.meta.read_only or spec.meta.interrupt:
            return
        r = self._reservation(device.name)
        if r is not None and r.holder != caller:
            raise DeviceReserved(f"{device.name} is reserved by {r.holder!r}.", details=r.to_dict())

    def reserve(self, device_name: str, *, caller: str, ttl_s: float) -> Reservation:
        """Hold a device across several calls so other callers cannot interleave mutating commands."""
        self.device(device_name)
        if caller == ANONYMOUS:
            raise InvalidArgument("Reservations need a caller identity. Send the X-Caller header (or set caller in MCP).")
        if not 1 <= ttl_s <= 3600:
            raise InvalidArgument("ttl_s must be between 1 and 3600 seconds.")
        current = self._reservation(device_name)
        if current is not None and current.holder != caller:
            raise DeviceReserved(f"{device_name} is reserved by {current.holder!r}.", details=current.to_dict())
        r = Reservation(holder=caller, expires_at=utcnow() + timedelta(seconds=ttl_s))
        self._reservations[device_name] = r
        self.audit.record(event="reserve", caller=caller, device=device_name, outcome="ok", result=r.to_dict())
        return r

    def release(self, device_name: str, *, caller: str, force: bool = False) -> None:
        self.device(device_name)
        current = self._reservation(device_name)
        if current is None:
            return
        if current.holder != caller and not force:
            raise DeviceReserved(f"{device_name} is reserved by {current.holder!r}, not {caller!r}.", details=current.to_dict())
        del self._reservations[device_name]
        self.audit.record(event="release", caller=caller, device=device_name, outcome="ok", result={"forced": force})

    # -- idempotency ---------------------------------------------------------------

    @staticmethod
    def _fingerprint(device: str, capability: str, args: dict[str, Any], caller: str) -> str:
        blob = json.dumps([device, capability, args, caller], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def _idempotent_replay(self, key: str, fingerprint: str) -> asyncio.Task[Invocation] | None:
        now = time.monotonic()
        for k in [k for k, (_, ts, _) in self._idempotency.items() if now - ts > self._idempotency_ttl]:
            del self._idempotency[k]
        entry = self._idempotency.get(key)
        if entry is None:
            return None
        if entry[0] != fingerprint:
            raise InvalidArgument(
                "Idempotency key was already used for a different request.",
                recoverable=False,
                suggested_action="Use a fresh idempotency key for each distinct action.",
            )
        return entry[2]


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)

