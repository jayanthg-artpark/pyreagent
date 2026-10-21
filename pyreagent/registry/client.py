"""Background task that keeps a device server announced in the registry."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from ..core.hub import DeviceHub

log = logging.getLogger("pyreagent.registry")


class RegistryAnnouncer:
    def __init__(self, registry_url: str, *, public_url: str, hub: "DeviceHub", interval_s: float = 10.0) -> None:
        self.registry_url = registry_url.rstrip("/")
        self.public_url = public_url
        self.hub = hub
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None

    def payload(self) -> dict:
        return {
            "server_id": self.hub.name,
            "url": self.public_url,
            "devices": self.hub.describe()["devices"],
            "ttl_s": self.interval_s * 3,
        }

    async def announce_once(self, client: httpx.AsyncClient) -> None:
        response = await client.post(f"{self.registry_url}/servers", json=self.payload())
        response.raise_for_status()

    async def _loop(self) -> None:
        async with httpx.AsyncClient(timeout=5) as client:
            failing = False
            while True:
                try:
                    await self.announce_once(client)
                    if failing:
                        log.info("registry %s reachable again", self.registry_url)
                    failing = False
                except httpx.HTTPError as exc:
                    if not failing:
                        log.warning("cannot announce to registry %s: %s", self.registry_url, exc)
                    failing = True
                await asyncio.sleep(self.interval_s)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="pyreagent-registry-announcer")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                await client.delete(f"{self.registry_url}/servers/{self.hub.name}")
        except httpx.HTTPError:
            pass
