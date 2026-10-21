"""HTTP API generated from the devices' capability interfaces.

One route per capability, ``POST /devices/{device}/{capability}``, with the
request body schema taken from the capability signature, so a new device type
or driver needs no hand-written endpoints. The same app serves MCP at ``/mcp``.
"""

import hmac
import inspect
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Optional

from fastapi import Body, FastAPI, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from starlette.routing import Route

from .. import __version__
from ..core.capability import CapabilitySpec
from ..core.device import Device
from ..core.errors import Forbidden, InvalidArgument, NotFound, PyreagentError
from ..core.hub import ANONYMOUS, DeviceHub, Invocation
from ..core.jobs import JobStatus
from ..labware import DEFAULT_CATALOG
from .mcp import build_mcp_server

log = logging.getLogger("pyreagent.server")


class ErrorInfo(BaseModel):
    code: str
    message: str
    recoverable: bool
    suggested_action: str
    details: dict[str, Any] = {}


class ErrorBody(BaseModel):
    error: ErrorInfo


class ReserveBody(BaseModel):
    ttl_s: float = Field(default=300, ge=1, le=3600, description="Reservation lifetime in seconds; reserve again to extend.")


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorBody, "description": text}
    for status, text in {
        404: "Unknown device or capability",
        409: "Device busy, faulted, or needs attention",
        422: "Invalid arguments",
        423: "Device reserved by another caller",
        503: "Device offline",
    }.items()
}

CALLER_HEADER = Header(default=None, alias="X-Caller", description="Who is calling (agent or user name). Recorded in the audit log.")


def _error_response(err: PyreagentError) -> JSONResponse:
    return JSONResponse(status_code=err.http_status, content={"error": jsonable_encoder(err.to_dict())})


def _caller(value: Optional[str]) -> str:
    return value or ANONYMOUS


def create_app(
    hub: DeviceHub,
    *,
    operator_token: Optional[str] = None,
    registry_url: Optional[str] = None,
    public_url: Optional[str] = None,
    heartbeat_s: float = 10.0,
    enable_mcp: bool = True,
    allowed_hosts: Optional[list[str]] = None,
) -> FastAPI:
    """Build the device server.

    Args:
        operator_token: If set, force-releasing reservations and (dis)connecting devices
            require ``X-Operator-Token``. Leave unset only on a trusted network.
        registry_url, public_url: Announce this server to a registry under ``public_url``.
        allowed_hosts: Host headers accepted by the MCP endpoint (DNS-rebinding protection),
            e.g. ``["localhost:*", "bench-1.lab:*"]``. None disables the check.
    """
    session_manager: Optional[StreamableHTTPSessionManager] = None
    if enable_mcp:
        security = (
            TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts, allowed_origins=[])
            if allowed_hosts
            else TransportSecuritySettings(enable_dns_rebinding_protection=False)
        )
        session_manager = StreamableHTTPSessionManager(
            app=build_mcp_server(hub), json_response=True, stateless=True, security_settings=security
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            await hub.start()
            stack.push_async_callback(hub.stop)
            if session_manager is not None:
                await stack.enter_async_context(session_manager.run())
            if registry_url and public_url:
                from ..registry.client import RegistryAnnouncer

                announcer = RegistryAnnouncer(registry_url, public_url=public_url, hub=hub, interval_s=heartbeat_s)
                announcer.start()
                stack.push_async_callback(announcer.stop)
            yield

    app = FastAPI(
        title=f"pyreagent device server: {hub.name}",
        version=__version__,
        description=(
            "Agent-safe lab instrument API. Start with GET /devices. Long-running capabilities return 202 "
            "with a job; poll GET /jobs/{id}?wait_s=10. Errors are {error: {code, message, recoverable, "
            "suggested_action, details}}. The same tools are available over MCP at /mcp."
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(PyreagentError)
    async def _pyreagent_error(request: Request, exc: PyreagentError) -> JSONResponse:
        return _error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'][1:]) or 'body'}: {e['msg']}" for e in exc.errors())
        return _error_response(InvalidArgument(f"Invalid request: {problems}", details={"errors": jsonable_encoder(exc.errors())}))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            err: PyreagentError = NotFound(f"No route {request.method} {request.url.path}.",
                                           suggested_action="Call GET /devices to see devices and their capability routes.")
        else:
            err = PyreagentError(str(exc.detail), recoverable=False, suggested_action="Check the request against GET /openapi.json.")
            err.code, err.http_status = "http_error", exc.status_code
        return _error_response(err)

    def operator(request: Request) -> str:
        if operator_token:
            supplied = request.headers.get("x-operator-token", "")
            if not hmac.compare_digest(supplied.encode(), operator_token.encode()):
                raise Forbidden("This endpoint needs a valid X-Operator-Token.")
        return request.headers.get("x-caller") or "operator"

    # -- discovery --------------------------------------------------------------

    @app.get("/health", tags=["server"])
    async def health() -> dict[str, Any]:
        return {"status": "ok", "server": hub.name, "devices": {n: d.state.value for n, d in hub.devices.items()}}

    @app.get("/devices", tags=["server"], summary="List devices with state, limits and capabilities")
    async def list_devices() -> dict[str, Any]:
        return hub.describe()

    @app.get("/devices/{device}", tags=["server"], responses=ERROR_RESPONSES)
    async def get_device(device: str) -> dict[str, Any]:
        return hub.describe_device(device)

    @app.get("/labware", tags=["server"])
    async def list_labware() -> dict[str, Any]:
        return {"labware": [lw.model_dump() for lw in DEFAULT_CATALOG.list()]}

    # -- reservations and connection --------------------------------------------

    @app.post("/devices/{device}/reservation", tags=["reservations"], responses=ERROR_RESPONSES)
    async def reserve(device: str, body: ReserveBody = Body(default_factory=ReserveBody), x_caller: Optional[str] = CALLER_HEADER) -> dict[str, Any]:
        """Hold the device for a multi-step sequence; others get 423 for mutating commands until release or expiry."""
        return hub.reserve(device, caller=_caller(x_caller), ttl_s=body.ttl_s).to_dict()

    @app.delete("/devices/{device}/reservation", tags=["reservations"], responses=ERROR_RESPONSES)
    async def release(device: str, request: Request, force: bool = False, x_caller: Optional[str] = CALLER_HEADER) -> dict[str, Any]:
        """Release your reservation. ``force=true`` (operator only) releases someone else's."""
        caller = operator(request) if force else _caller(x_caller)
        hub.release(device, caller=caller, force=force)
        return {"released": device}

    @app.post("/devices/{device}/connect", tags=["operator"], responses=ERROR_RESPONSES)
    async def connect(device: str, request: Request) -> dict[str, Any]:
        return await hub.connect_device(device, caller=operator(request))

    @app.post("/devices/{device}/disconnect", tags=["operator"], responses=ERROR_RESPONSES)
    async def disconnect(device: str, request: Request) -> dict[str, Any]:
        return await hub.disconnect_device(device, caller=operator(request))

    # -- capabilities (generated) -----------------------------------------------

    for device in hub.devices.values():
        for spec in device.capability_specs().values():
            _add_capability_route(app, hub, device, spec)

    # -- jobs ---------------------------------------------------------------------

    @app.get("/jobs", tags=["jobs"])
    async def list_jobs(device: Optional[str] = None, status: Optional[JobStatus] = None, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        return {"jobs": [j.model_dump(mode="json") for j in hub.jobs.list(device=device, status=status, limit=limit)]}

    @app.get("/jobs/{job_id}", tags=["jobs"], responses=ERROR_RESPONSES)
    async def get_job(job_id: str, wait_s: float = Query(0, ge=0, le=30, description="Long-poll: wait up to this long for the job to finish.")) -> dict[str, Any]:
        return (await hub.jobs.wait(job_id, wait_s)).model_dump(mode="json")

    @app.post("/jobs/{job_id}/cancel", tags=["jobs"], responses=ERROR_RESPONSES)
    async def cancel_job(job_id: str, x_caller: Optional[str] = CALLER_HEADER) -> dict[str, Any]:
        job = await hub.jobs.cancel(job_id)
        hub.audit.record(event="cancel", caller=_caller(x_caller), device=job.device, capability=job.capability, job_id=job.id, outcome=job.status.value)
        return job.model_dump(mode="json")

    # -- audit ----------------------------------------------------------------------

    @app.get("/audit", tags=["audit"])
    async def audit(device: Optional[str] = None, caller: Optional[str] = None, event: Optional[str] = None, limit: int = Query(100, ge=1, le=2000)) -> dict[str, Any]:
        return {"records": [r.model_dump(mode="json") for r in hub.audit.query(device=device, caller=caller, event=event, limit=limit)]}

    if session_manager is not None:
        app.router.routes.append(Route("/mcp", endpoint=StreamableHTTPASGIApp(session_manager)))

    return app


def _add_capability_route(app: FastAPI, hub: DeviceHub, device: Device[Any], spec: CapabilitySpec) -> None:
    model = spec.args_model
    has_fields = bool(model.model_fields)
    required = any(f.is_required() for f in model.model_fields.values())
    device_name, capability_name = device.name, spec.name

    async def endpoint(**kwargs: Any) -> JSONResponse:
        body: Optional[BaseModel] = kwargs.get("body")
        invocation: Invocation = await hub.invoke(
            device_name,
            capability_name,
            body.model_dump(mode="json") if body is not None else {},
            caller=_caller(kwargs.get("x_caller")),
            idempotency_key=kwargs.get("idempotency_key"),
        )
        status = 202 if invocation.status == "accepted" else 200
        return JSONResponse(status_code=status, content=invocation.model_dump(mode="json"))

    params = []
    if has_fields:
        params.append(
            inspect.Parameter(
                "body", inspect.Parameter.KEYWORD_ONLY,
                annotation=model if required else Optional[model],
                default=Body(...) if required else Body(default=None),
            )
        )
    params += [
        inspect.Parameter("x_caller", inspect.Parameter.KEYWORD_ONLY, annotation=Optional[str], default=CALLER_HEADER),
        inspect.Parameter(
            "idempotency_key", inspect.Parameter.KEYWORD_ONLY, annotation=Optional[str],
            default=Header(default=None, alias="Idempotency-Key", description="Repeat a request safely: same key, same outcome."),
        ),
    ]
    endpoint.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]

    flags = []
    if spec.meta.long_running:
        flags.append("**Long-running**: returns 202 with a job; poll `GET /jobs/{id}`.")
    if spec.meta.read_only:
        flags.append("**Read-only**: allowed while busy or reserved.")
    if spec.meta.interrupt:
        flags.append("**Safety stop**: always allowed.")

    app.add_api_route(
        f"/devices/{device_name}/{capability_name}",
        endpoint,
        methods=["POST"],
        name=f"{device_name}__{capability_name}",
        operation_id=f"{device_name}__{capability_name}",
        summary=spec.summary,
        description=spec.meta.description + ("\n\n" + "\n\n".join(flags) if flags else ""),
        tags=[device_name],
        response_model=Invocation,
        responses={202: {"model": Invocation, "description": "Accepted as a job"}, **ERROR_RESPONSES},
    )
