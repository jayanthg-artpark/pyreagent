"""Command line: ``pyreagent serve|demo|registry|drivers``."""

from __future__ import annotations

import argparse
import logging
import sys
from importlib import resources
from pathlib import Path


def _serve(config: Path, host: str | None, port: int | None, log_level: str) -> None:
    import uvicorn

    from .config import load_config
    from .server import create_app

    settings, hub = load_config(config)
    host = host or settings.host
    port = port or settings.port
    if settings.operator_token is None:
        logging.getLogger("pyreagent").warning(
            "No operator token set (%s): anyone who can reach this server can approve confirmations.",
            settings.operator_token_env,
        )
    app = create_app(
        hub,
        operator_token=settings.operator_token,
        registry_url=settings.registry_url,
        public_url=settings.public_url or f"http://{host}:{port}",
        heartbeat_s=settings.heartbeat_s,
        allowed_hosts=settings.mcp_allowed_hosts,
    )
    uvicorn.run(app, host=host, port=port, log_level=log_level)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pyreagent", description="Agent-safe lab device servers.")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run a device server from a TOML config.")
    serve.add_argument("config", type=Path)
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)

    demo = sub.add_parser("demo", help="Run a server with one simulated device of every type.")
    demo.add_argument("--host")
    demo.add_argument("--port", type=int)

    registry = sub.add_parser("registry", help="Run the device registry.")
    registry.add_argument("--host", default="127.0.0.1")
    registry.add_argument("--port", type=int, default=9000)

    sub.add_parser("drivers", help="List device types and the brands with drivers for each.")

    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")

    if args.command == "serve":
        _serve(args.config, args.host, args.port, args.log_level)
    elif args.command == "demo":
        with resources.as_file(resources.files("pyreagent.configs") / "simulated_lab.toml") as path:
            _serve(path, args.host, args.port, args.log_level)
    elif args.command == "registry":
        import uvicorn

        from .registry import create_registry_app

        uvicorn.run(create_registry_app(), host=args.host, port=args.port, log_level=args.log_level)
    elif args.command == "drivers":
        from .devices import available_drivers

        for device_type, brands in available_drivers().items():
            print(f"{device_type}: {', '.join(brands)}")


if __name__ == "__main__":
    main(sys.argv[1:])
