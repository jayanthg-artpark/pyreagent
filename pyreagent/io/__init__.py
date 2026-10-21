"""Transports: how bytes reach an instrument. Drivers pick one; protocols live in the drivers."""

from .http import HttpBridge
from .mock import MockTransport
from .serial import SerialTransport
from .tcp import TcpTransport
from .transport import Transport


def line_transport(
    *, port: str | None = None, host: str | None = None, tcp_port: int | None = None, baudrate: int = 9600, **serial_options: object
) -> Transport:
    """Build a serial transport (``port="COM3"``) or a TCP one (``host=..., tcp_port=...``) from driver options."""
    if port and host:
        raise ValueError("give either a serial port or a host, not both")
    if port:
        return SerialTransport(port, baudrate=baudrate, **serial_options)  # type: ignore[arg-type]
    if host:
        if tcp_port is None:
            raise ValueError("tcp_port is required with host")
        return TcpTransport(host, tcp_port)
    raise ValueError("driver needs a serial port (port=...) or a network address (host=..., tcp_port=...)")


__all__ = ["HttpBridge", "MockTransport", "SerialTransport", "TcpTransport", "Transport", "line_transport"]
