"""Device server: HTTP routes and MCP tools generated from capability interfaces."""

from .app import create_app
from .mcp import build_mcp_server

__all__ = ["build_mcp_server", "create_app"]
