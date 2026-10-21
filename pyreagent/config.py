"""Load a device server from a TOML file.

    [server]
    name = "bench-1"
    port = 8000
    public_url = "http://bench-1.lab:8000"     # how others reach this server
    registry_url = "http://registry.lab:9000"  # optional
    audit_log = "logs/bench-1.audit.jsonl"     # relative to this file

    [[devices]]
    name = "balance-1"
    type = "balance"                 # folder under pyreagent/devices/
    driver = "mettler_toledo"        # brand folder (or "brand.module:Class", or "pkg.mod:Class")
    driver_options = { port = "COM3", baudrate = 9600 }   # passed to the driver
    options = {}                     # passed to the device (limits etc.)
    confirm = {}                     # per-capability confirmation overrides
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .core.audit import AuditLog
from .core.device import Device
from .core.hub import DeviceHub
from .devices import device_class, resolve_driver
from .labware import DEFAULT_CATALOG, Labware


class ConfigError(ValueError):
    pass


@dataclass
class ServerSettings:
    name: str = "pyreagent"
    host: str = "127.0.0.1"
    port: int = 8000
    public_url: str | None = None
    registry_url: str | None = None
    heartbeat_s: float = 10.0
    audit_log: str | None = None
    operator_token_env: str = "PYREAGENT_OPERATOR_TOKEN"
    mcp_allowed_hosts: list[str] | None = None
    confirmation_ttl_s: float = 600
    base_dir: Path = field(default_factory=Path.cwd, repr=False)

    @property
    def operator_token(self) -> str | None:
        return os.environ.get(self.operator_token_env) or None

    @property
    def audit_path(self) -> Path | None:
        return self.base_dir / self.audit_log if self.audit_log else None


_DEVICE_KEYS = {"name", "type", "driver", "driver_options", "options", "confirm"}


def build_device(entry: dict[str, Any]) -> Device[Any]:
    unknown = set(entry) - _DEVICE_KEYS
    if unknown:
        raise ConfigError(f"unknown keys {sorted(unknown)}; allowed: {sorted(_DEVICE_KEYS)}")
    for key in ("name", "type", "driver"):
        if key not in entry:
            raise ConfigError(f"missing {key!r}")
    try:
        cls = device_class(entry["type"])
        backend = resolve_driver(entry["type"], entry["driver"])(**entry.get("driver_options", {}))
        return cls(entry["name"], backend, confirm=entry.get("confirm"), **entry.get("options", {}))
    except (TypeError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc


def load_config(path: str | Path) -> tuple[ServerSettings, DeviceHub]:
    path = Path(path)
    with path.open("rb") as fh:
        data = tomllib.load(fh)

    server_data = data.get("server", {})
    allowed = {f.name for f in fields(ServerSettings)} - {"base_dir"}
    unknown = set(server_data) - allowed
    if unknown:
        raise ConfigError(f"{path}: unknown [server] keys {sorted(unknown)}")
    settings = ServerSettings(**server_data, base_dir=path.resolve().parent)

    for index, item in enumerate(data.get("labware", [])):
        try:
            DEFAULT_CATALOG.add(Labware(**item))
        except Exception as exc:
            raise ConfigError(f"{path}: labware[{index}]: {exc}") from exc

    devices = []
    for index, entry in enumerate(data.get("devices", [])):
        try:
            devices.append(build_device(entry))
        except ConfigError as exc:
            raise ConfigError(f"{path}: devices[{index}] ({entry.get('name', '?')}): {exc}") from exc

    hub = DeviceHub(
        devices,
        name=settings.name,
        audit=AuditLog(settings.audit_path),
        confirmation_ttl_s=settings.confirmation_ttl_s,
    )
    return settings, hub
