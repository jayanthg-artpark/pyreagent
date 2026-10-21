"""MCP tools generated from the same capabilities."""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest
import uvicorn
from mcp import Client

from conftest import DEVICE_NAMES, make_devices
from pyreagent.core.hub import DeviceHub
from pyreagent.server import build_mcp_server, create_app


def payload(result) -> dict:
    return json.loads(result.content[0].text)


async def test_tools_are_generated(hub: DeviceHub):
    async with Client(build_mcp_server(hub)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert {"list_devices", "get_job", "get_confirmation", "reserve_device"} <= set(tools)
    assert "approve_confirmation" not in tools  # approvals are for humans only
    weigh = tools["balance-1__weigh"]
    assert weigh.annotations.read_only_hint is True
    assert "timeout_s" in weigh.input_schema["properties"]
    dispense = tools["pump-1__dispense"]
    assert dispense.annotations.destructive_hint is True
    assert "confirmation_id" in dispense.input_schema["properties"]
    assert "Long-running" in dispense.description and "human approval" in dispense.description
    assert "confirmation_id" not in tools["balance-1__weigh"].input_schema["properties"]


async def test_agent_flow_over_mcp(hub: DeviceHub):
    async with Client(build_mcp_server(hub)) as client:
        devices = payload(await client.call_tool("list_devices", {}))
        assert {d["name"] for d in devices["devices"]} == DEVICE_NAMES

        result = await client.call_tool("balance-1__tare", {})
        assert not result.is_error and payload(result)["status"] == "completed"

        args = {"volume_ml": 1.0, "rate_ml_min": 10.0}
        gated = await client.call_tool("pump-1__dispense", args)
        assert gated.is_error
        error = payload(gated)["error"]
        assert error["code"] == "confirmation_required"
        cid = error["details"]["confirmation_id"]

        hub.decide_confirmation(cid, approve=True, operator="alice")  # the human, outside MCP
        status = payload(await client.call_tool("get_confirmation", {"confirmation_id": cid}))
        assert status["status"] == "approved"

        accepted = payload(await client.call_tool("pump-1__dispense", {**args, "confirmation_id": cid}))
        assert accepted["status"] == "accepted"
        job = payload(await client.call_tool("get_job", {"job_id": accepted["job"]["id"], "wait_s": 5}))
        assert job["status"] == "succeeded"

        bad = await client.call_tool("get_job", {"job_id": "x", "wait_s": 99})
        assert bad.is_error and payload(bad)["error"]["code"] == "invalid_argument"
        unknown = await client.call_tool("no_such_tool", {})
        assert unknown.is_error and payload(unknown)["error"]["code"] == "not_found"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running_server():
    port = _free_port()
    app = create_app(DeviceHub(make_devices(), name="http-lab"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


async def test_mcp_over_streamable_http(running_server: str):
    async with Client(f"{running_server}/mcp") as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert "shaker-1__shake" in names
        result = await client.call_tool("shaker-1__status", {})
        assert payload(result)["result"] == {"shaking": False, "rpm": 0.0}
