"""Thin async client for the Opentrons robot-server HTTP API (OT-2 and Flex, port 31950).

Endpoints used (robot-server, API version header ``Opentrons-Version: *``):

    GET   /health                       robot model, name, software version
    GET   /instruments                  attached pipettes: mount, instrumentName, data.channels/min_volume/max_volume
    GET   /modules                      attached modules: moduleType, moduleModel, serialNumber, live data
    GET   /deck_configuration           Flex fixtures (trash bins, staging areas, module cutouts)
    POST  /runs/{id}/labware_definitions  custom labware for this run
    POST  /runs                         create an empty run (409 RunAlreadyActive if another run is current)
    POST  /runs/{id}/commands           enqueue a command; intent "setup" executes immediately on an idle run
          ?waitUntilComplete=true&timeout=<ms>
    GET   /runs/{id}/commands/{cmdId}   poll a command that outlived the wait timeout
    GET   /runs/{id}                    run status ("stop-requested" until a stop has finished)
    POST  /runs/{id}/actions            {"actionType": "stop"}
    PATCH /runs/{id}                    {"current": false} releases the robot
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

import httpx

from ...core.errors import CommunicationError, HardwareError, NeedsAttention, PyreagentError

DEFAULT_PORT = 31950


def _http_error(response: httpx.Response) -> PyreagentError:
    try:
        errors = response.json().get("errors") or []
    except ValueError:
        errors = []
    first = errors[0] if errors else {}
    kind = str(first.get("id", ""))
    detail = first.get("detail") or first.get("title") or response.text[:200]
    details = {"http_status": response.status_code, "opentrons_error": kind, "detail": detail}
    if kind == "RunAlreadyActive":
        return NeedsAttention(
            "Another run is active on the robot (for example one started from the Opentrons App).",
            suggested_action="Ask a human to finish or close that run in the Opentrons App, then reconnect this device.",
            details=details,
        )
    if kind == "RunStopped":
        return HardwareError("The robot's run has been stopped; this device must be reset.", details=details)
    if "estop" in kind.lower():
        return NeedsAttention("The robot's emergency stop is engaged.",
                              suggested_action="Ask a human to release the E-stop, then reset the device.", details=details)
    return CommunicationError(f"Robot refused the request (HTTP {response.status_code} {kind}): {detail}",
                              recoverable=False, details=details)


# Engine errors that mean "the setup needs something first", not "the hardware is in an unknown state".
SETUP_ERRORS: dict[str, tuple[str, str]] = {
    "MustHomeError": ("The robot must be homed before it can move.", "Call the home capability, then retry."),
    "ThermocyclerNotOpenError": ("The thermocycler lid is closed.", "Open the thermocycler lid, then retry."),
    "HeaterShakerLabwareLatchNotOpenError": ("The heater-shaker latch is closed.", "Open the latch, then retry."),
    "GripperNotAttachedError": ("No gripper is attached.", "Move the labware by hand and record it with use_gripper=false."),
    "ModuleNotAttachedError": ("The module is not attached to the robot.", "Ask a human to check the module's USB and power, then reset."),
}


def command_error(command_type: str, error: Mapping[str, Any]) -> PyreagentError:
    kind = str(error.get("errorType", "UnknownError"))
    detail = str(error.get("detail", ""))
    details = {"command": command_type, "error_type": kind, "error_code": error.get("errorCode"), "detail": detail}
    if kind in SETUP_ERRORS:
        message, action = SETUP_ERRORS[kind]
        return NeedsAttention(message, recoverable=True, suggested_action=action, details=details)
    return HardwareError(f"Robot failed {command_type}: {kind}: {detail}", details=details)


class OpentronsClient:
    def __init__(
        self,
        host: str,
        *,
        port: int = DEFAULT_PORT,
        headers: Mapping[str, str] | None = None,
        request_timeout_s: float = 10.0,
        poll_interval_s: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = f"http://{host}:{port}"
        self.headers = {"Opentrons-Version": "*", **(headers or {})}
        self.request_timeout_s = request_timeout_s
        self.poll_interval_s = poll_interval_s
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    async def open(self) -> None:
        self._http = httpx.AsyncClient(base_url=self.base_url, headers=self.headers, timeout=self.request_timeout_s,
                                       transport=self._transport)

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _request(self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None,
                       timeout_s: float | None = None) -> dict[str, Any]:
        if self._http is None:
            raise ConnectionError(f"not connected to {self.base_url}")
        try:
            response = await self._http.request(method, path, json=json, params=params, timeout=timeout_s or self.request_timeout_s)
        except httpx.TimeoutException as exc:
            raise TimeoutError(f"{self.base_url}{path} timed out") from exc
        except httpx.TransportError as exc:
            raise ConnectionError(f"{self.base_url}{path}: {exc}") from exc
        if response.status_code >= 400:
            raise _http_error(response)
        return response.json() if response.content else {}

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def instruments(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/instruments"))["data"]

    async def modules(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/modules"))["data"]

    async def deck_configuration(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/deck_configuration"))["data"]["cutoutFixtures"]

    async def create_run(self) -> str:
        return (await self._request("POST", "/runs", json={"data": {}}))["data"]["id"]

    async def add_labware_definition(self, run_id: str, definition: Mapping[str, Any]) -> str:
        """Make a custom labware definition loadable in this run; returns its definitionUri."""
        return (await self._request("POST", f"/runs/{run_id}/labware_definitions", json={"data": dict(definition)}))["data"]["definitionUri"]

    async def run(self, run_id: str) -> dict[str, Any]:
        return (await self._request("GET", f"/runs/{run_id}"))["data"]

    async def stop_run(self, run_id: str) -> None:
        await self._request("POST", f"/runs/{run_id}/actions", json={"data": {"actionType": "stop"}})

    async def release_run(self, run_id: str) -> None:
        await self._request("PATCH", f"/runs/{run_id}", json={"data": {"current": False}})

    async def run_command(self, run_id: str, command_type: str, params: Mapping[str, Any], *, timeout_s: float) -> dict[str, Any]:
        """Execute one setup command and return its ``result``; raise a structured error if it fails."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        reply = await self._request(
            "POST", f"/runs/{run_id}/commands",
            json={"data": {"commandType": command_type, "params": dict(params), "intent": "setup"}},
            params={"waitUntilComplete": "true", "timeout": max(1, int(timeout_s * 1000))},
            timeout_s=timeout_s + self.request_timeout_s,
        )
        command = reply["data"]
        while command["status"] in ("queued", "running"):
            if loop.time() > deadline:
                raise TimeoutError(f"{command_type} still {command['status']} after {timeout_s:g}s")
            await asyncio.sleep(self.poll_interval_s)
            command = (await self._request("GET", f"/runs/{run_id}/commands/{command['id']}"))["data"]
        if command["status"] == "failed":
            raise command_error(command_type, command.get("error") or {})
        return command.get("result") or {}
