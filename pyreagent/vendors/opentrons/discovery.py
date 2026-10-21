"""Find Opentrons robots on the local network.

Robots advertise ``_http._tcp.local.`` on port 31950, with TXT ``robotModel``
("OT-3 Standard" on a Flex; an OT-2 may not send it). Each candidate is then
confirmed with ``GET /health``, as the Opentrons App does. Needs the optional
``zeroconf`` package: ``pip install 'pyreagent[opentrons]'``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from ...core.errors import InvalidArgument, NeedsAttention
from .client import DEFAULT_PORT

SERVICE = "_http._tcp.local."
MODELS = {"ot2": "OT-2 Standard", "flex": "OT-3 Standard"}


@dataclass(frozen=True)
class FoundRobot:
    name: str
    host: str
    port: int
    robot_model: str

    @property
    def robot(self) -> str:
        return "flex" if self.robot_model == MODELS["flex"] else "ot2"


async def _browse(timeout_s: float) -> list[tuple[str, str, int]]:
    try:
        from zeroconf import ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ImportError("Robot discovery needs zeroconf: pip install 'pyreagent[opentrons]'") from exc

    names: set[str] = set()

    def on_change(zeroconf, service_type: str, name: str, state_change) -> None:  # noqa: ANN001
        if state_change is ServiceStateChange.Added:
            names.add(name)

    aiozc = AsyncZeroconf()
    browser = AsyncServiceBrowser(aiozc.zeroconf, [SERVICE], handlers=[on_change])
    try:
        await asyncio.sleep(timeout_s)
        found = []
        for name in sorted(names):
            info = AsyncServiceInfo(SERVICE, name)
            if not await info.async_request(aiozc.zeroconf, 3000) or info.port != DEFAULT_PORT:
                continue
            ipv4 = [a for a in info.parsed_addresses() if ":" not in a]
            if ipv4:
                found.append((name.removesuffix("." + SERVICE), ipv4[0], info.port))
        return found
    finally:
        await browser.async_cancel()
        await aiozc.async_close()


async def confirm(candidates: list[tuple[str, str, int]], *, transport: httpx.AsyncBaseTransport | None = None) -> list[FoundRobot]:
    """Keep the candidates whose /health answers like an Opentrons robot."""
    robots = []
    async with httpx.AsyncClient(timeout=3, headers={"Opentrons-Version": "*"}, transport=transport) as client:
        for name, host, port in candidates:
            try:
                health = (await client.get(f"http://{host}:{port}/health")).json()
            except (httpx.HTTPError, ValueError):
                continue
            if health.get("robot_model") in MODELS.values():
                robots.append(FoundRobot(name=health.get("name") or name, host=host, port=port, robot_model=health["robot_model"]))
    return robots


async def discover(timeout_s: float = 3.0) -> list[FoundRobot]:
    return await confirm(await _browse(timeout_s))


def choose(robots: list[FoundRobot], *, robot: str, name: str | None = None) -> FoundRobot:
    """The one robot matching type (and name, if given), or a structured error."""
    matches = [r for r in robots if r.robot == robot and (name is None or r.name == name)]
    if not matches:
        raise NeedsAttention(f"No {robot} robot{f' named {name!r}' if name else ''} found on the network.",
                             suggested_action="Check the robot is on and on the same network, or set host explicitly.",
                             details={"found": [r.name for r in robots]})
    if len(matches) > 1:
        raise InvalidArgument(f"Several {robot} robots found; set robot_name or host in driver_options.",
                              details={"found": [r.name for r in matches]})
    return matches[0]
