"""HTTP API generated from capabilities."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import DEVICE_NAMES, make_devices
from pyreagent.core.hub import DeviceHub
from pyreagent.server import create_app

TOKEN = "s3cret"
AGENT = {"X-Caller": "agent"}
HUMAN = {"X-Caller": "alice", "X-Operator-Token": TOKEN}


@pytest.fixture
def client():
    hub = DeviceHub(make_devices(), name="test-lab")
    with TestClient(create_app(hub, operator_token=TOKEN)) as c:
        yield c


def test_discovery(client: TestClient):
    body = client.get("/devices").json()
    names = {d["name"]: d for d in body["devices"]}
    assert set(names) == DEVICE_NAMES
    pump = names["pump-1"]
    assert pump["state"] == "idle" and pump["simulated"] is True
    assert pump["limits"]["max_volume_ml"] == 10
    dispense = next(c for c in pump["capabilities"] if c["name"] == "dispense")
    assert dispense["long_running"] and not dispense["read_only"]
    assert "volume_ml" in dispense["parameters"]["properties"]
    assert client.get("/health").json()["devices"]["pump-1"] == "idle"


def test_openapi_is_generated_from_capabilities(client: TestClient):
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/devices/pump-1/dispense"]["post"]
    assert op["operationId"] == "pump-1__dispense"
    assert "Long-running" in op["description"]
    headers = {p["name"] for p in op["parameters"]}
    assert {"X-Caller", "Idempotency-Key"} <= headers
    assert "/devices/balance-1/weigh" in spec["paths"]


def test_call_and_structured_errors(client: TestClient):
    r = client.post("/devices/balance-1/tare", json={}, headers=AGENT)
    assert r.status_code == 200 and r.json()["status"] == "completed"
    r = client.post("/devices/balance-1/weigh", headers=AGENT)  # body optional when all args have defaults
    assert r.status_code == 200 and r.json()["result"]["grams"] == 0

    r = client.post("/devices/balance-1/weigh", json={"timeout_s": 999}, headers=AGENT)
    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "invalid_argument" and error["recoverable"] is True and error["suggested_action"]

    r = client.post("/devices/nope/weigh", json={})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_long_running_flow_over_http(client: TestClient):
    args = {"volume_ml": 2.0, "rate_ml_min": 10.0}
    r = client.post("/devices/pump-1/dispense", json=args, headers={**AGENT, "Idempotency-Key": "k1"})
    assert r.status_code == 202
    job_id = r.json()["job"]["id"]
    # A retry with the same key returns the same job instead of dispensing again.
    again = client.post("/devices/pump-1/dispense", json=args, headers={**AGENT, "Idempotency-Key": "k1"})
    assert again.status_code == 202 and again.json()["job"]["id"] == job_id

    job = client.get(f"/jobs/{job_id}", params={"wait_s": 5}).json()
    assert job["status"] == "succeeded", job
    assert job["result"]["moved_ml"] == pytest.approx(2.0)

    audit = client.get("/audit", params={"device": "pump-1"}).json()["records"]
    assert {"invoke", "job_finished"} <= {r["event"] for r in audit}
    assert client.get("/confirmations").status_code == 404


def test_reservation_endpoints(client: TestClient):
    assert client.post("/devices/shaker-1/reservation", json={"ttl_s": 60}, headers={"X-Caller": "a"}).status_code == 200
    r = client.post("/devices/shaker-1/start_shaking", json={"rpm": 500}, headers={"X-Caller": "b"})
    assert r.status_code == 423 and r.json()["error"]["code"] == "device_reserved"
    assert client.delete("/devices/shaker-1/reservation", params={"force": "true"}, headers={"X-Caller": "b"}).status_code == 403
    assert client.delete("/devices/shaker-1/reservation", params={"force": "true"}, headers=HUMAN).status_code == 200
    assert client.post("/devices/shaker-1/start_shaking", json={"rpm": 500}, headers={"X-Caller": "b"}).status_code == 200


def test_labware(client: TestClient):
    ids = {lw["id"] for lw in client.get("/labware").json()["labware"]}
    assert "generic_96_wellplate" in ids
