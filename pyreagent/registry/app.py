"""Registry: where device servers announce themselves and orchestrators find devices.

Device servers POST their description every few seconds. The orchestrator
(Protocol Studio) queries ``GET /devices`` instead of hard-coding addresses.
A server that stops heartbeating is reported ``stale``, then dropped.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .. import __version__
from ..core.jobs import utcnow


class Announcement(BaseModel):
    server_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    url: str = Field(description="Base URL other machines use to reach the device server.")
    devices: list[dict[str, Any]] = Field(description="The server's GET /devices payload.")
    ttl_s: float = Field(default=30, gt=0, le=3600, description="Report stale if no announcement within this many seconds.")


class _Entry(BaseModel):
    announcement: Announcement
    first_seen: datetime
    last_seen: datetime

    def health(self, now: datetime) -> str:
        return "ok" if now - self.last_seen <= timedelta(seconds=self.announcement.ttl_s) else "stale"


def create_registry_app(*, drop_after_ttls: float = 10) -> FastAPI:
    app = FastAPI(title="pyreagent registry", version=__version__,
                  description="Device servers announce here; orchestrators and agents discover devices here.")
    entries: dict[str, _Entry] = {}

    def sweep() -> datetime:
        now = utcnow()
        for sid in [s for s, e in entries.items() if now - e.last_seen > timedelta(seconds=e.announcement.ttl_s * drop_after_ttls)]:
            del entries[sid]
        return now

    def server_view(entry: _Entry, now: datetime) -> dict[str, Any]:
        a = entry.announcement
        return {
            "server_id": a.server_id, "url": a.url, "health": entry.health(now),
            "first_seen": entry.first_seen.isoformat(), "last_seen": entry.last_seen.isoformat(),
            "devices": [d.get("name") for d in a.devices],
        }

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "servers": len(entries)}

    @app.post("/servers")
    async def announce(body: Announcement) -> dict[str, Any]:
        """Register or refresh a device server (also the heartbeat)."""
        now = utcnow()
        previous = entries.get(body.server_id)
        entries[body.server_id] = _Entry(announcement=body, first_seen=previous.first_seen if previous else now, last_seen=now)
        return server_view(entries[body.server_id], now)

    @app.get("/servers")
    async def list_servers() -> dict[str, Any]:
        now = sweep()
        return {"servers": [server_view(e, now) for e in entries.values()]}

    @app.delete("/servers/{server_id}")
    async def remove_server(server_id: str) -> dict[str, Any]:
        if entries.pop(server_id, None) is None:
            raise HTTPException(404, f"unknown server {server_id!r}")
        return {"removed": server_id}

    @app.get("/devices")
    async def list_devices(type: str | None = None, capability: str | None = None, include_stale: bool = False) -> dict[str, Any]:
        """Every device across all servers, with the URL to call it on. Filter by type or capability name."""
        now = sweep()
        devices = []
        for entry in entries.values():
            health = entry.health(now)
            if health != "ok" and not include_stale:
                continue
            a = entry.announcement
            for d in a.devices:
                if type and d.get("type") != type:
                    continue
                caps = [c.get("name") for c in d.get("capabilities", [])]
                if capability and capability not in caps:
                    continue
                devices.append({
                    "name": d.get("name"), "type": d.get("type"), "state": d.get("state"),
                    "simulated": d.get("simulated"), "limits": d.get("limits"), "capabilities": caps,
                    "server_id": a.server_id, "server_url": a.url, "server_health": health,
                    "device_url": f"{a.url.rstrip('/')}/devices/{d.get('name')}",
                    "mcp_url": f"{a.url.rstrip('/')}/mcp",
                })
        return {"devices": devices}

    return app
