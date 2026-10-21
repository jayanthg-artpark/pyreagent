from __future__ import annotations

import time
from importlib import resources

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import DEVICE_NAMES, make_devices
from pyreagent.config import ConfigError, build_device, load_config
from pyreagent.core.hub import DeviceHub
from pyreagent.registry import RegistryAnnouncer, create_registry_app


def test_registry_discovery_and_staleness():
    client = TestClient(create_registry_app())
    hub = DeviceHub(make_devices(), name="bench-1")
    announcement = RegistryAnnouncer("http://registry", public_url="http://bench-1:8000", hub=hub).payload()
    assert client.post("/servers", json=announcement).status_code == 200

    devices = client.get("/devices").json()["devices"]
    assert {d["name"] for d in devices} == DEVICE_NAMES
    pumps = client.get("/devices", params={"type": "pump"}).json()["devices"]
    assert [d["name"] for d in pumps] == ["pump-1"]
    assert pumps[0]["device_url"] == "http://bench-1:8000/devices/pump-1"
    assert pumps[0]["mcp_url"] == "http://bench-1:8000/mcp"
    readers = client.get("/devices", params={"capability": "read_absorbance"}).json()["devices"]
    assert [d["name"] for d in readers] == ["reader-1"]

    client.post("/servers", json={**announcement, "server_id": "bench-2", "ttl_s": 0.01})
    time.sleep(0.05)
    servers = {s["server_id"]: s["health"] for s in client.get("/servers").json()["servers"]}
    assert servers == {"bench-1": "ok", "bench-2": "stale"}
    assert len(client.get("/devices").json()["devices"]) == len(DEVICE_NAMES)
    assert len(client.get("/devices", params={"include_stale": True}).json()["devices"]) == 2 * len(DEVICE_NAMES)

    assert client.delete("/servers/bench-1").status_code == 200
    assert client.delete("/servers/bench-1").status_code == 404


async def test_announcer_posts_to_registry():
    app = create_registry_app()
    hub = DeviceHub(make_devices(), name="bench-1")
    announcer = RegistryAnnouncer("http://registry", public_url="http://bench-1:8000", hub=hub)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        await announcer.announce_once(client)
        servers = (await client.get("http://registry/servers")).json()["servers"]
    assert servers[0]["server_id"] == "bench-1" and set(servers[0]["devices"]) == DEVICE_NAMES


def test_packaged_simulated_lab_config_loads():
    with resources.as_file(resources.files("pyreagent.configs") / "simulated_lab.toml") as path:
        settings, hub = load_config(path)
    assert settings.name == "simulated-lab"
    from pyreagent.devices import DEVICE_TYPES

    assert {d.device_type for d in hub.devices.values()} == set(DEVICE_TYPES)


def test_config_errors_are_specific():
    with pytest.raises(ConfigError, match="no driver"):
        build_device({"name": "b", "type": "balance", "driver": "acme"})
    with pytest.raises(ConfigError, match="unknown device type"):
        build_device({"name": "b", "type": "centrifuge", "driver": "simulated"})
    with pytest.raises(ConfigError, match="unknown keys"):
        build_device({"name": "b", "type": "balance", "driver": "simulated", "port": "COM3"})
    with pytest.raises(ConfigError, match="max_volume_ml"):
        build_device({"name": "p", "type": "pump", "driver": "simulated"})
    device = build_device({"name": "bal", "type": "balance", "driver": "mettler_toledo", "driver_options": {"port": "COM3"}})
    assert device.backend.transport.describe() == {"transport": "serial", "port": "COM3", "baudrate": 9600}
