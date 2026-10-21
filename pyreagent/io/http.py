"""JSON-over-HTTP bridge, e.g. an ESP32 that fronts an in-house instrument.

Not a byte stream: the bridge firmware exposes endpoints and the driver calls
them. ``HttpBridge`` adds timeouts, one-at-a-time access, and maps transport
failures onto the exceptions ``translate_exception`` understands.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx


class HttpBridge:
    def __init__(self, base_url: str, *, timeout_s: float = 5.0, headers: dict[str, str] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.headers = headers or {}
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_s, headers=self.headers)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def request(self, method: str, path: str, *, json: Any = None, timeout_s: float | None = None) -> Any:
        if self._client is None:
            raise ConnectionError(f"bridge {self.base_url} is not open")
        async with self._lock:
            try:
                response = await self._client.request(method, path, json=json, timeout=timeout_s or self.timeout_s)
            except httpx.TimeoutException as exc:
                raise TimeoutError(f"{self.base_url}{path} timed out") from exc
            except httpx.TransportError as exc:
                raise ConnectionError(f"{self.base_url}{path}: {exc}") from exc
        if response.status_code >= 400:
            raise ConnectionError(f"{self.base_url}{path} returned HTTP {response.status_code}: {response.text[:200]}")
        return response.json() if response.content else None

    def describe(self) -> dict[str, Any]:
        return {"transport": "http", "base_url": self.base_url}
