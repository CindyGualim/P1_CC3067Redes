"""Errors raised by the MCP layer, above plain JSON-RPC failures."""

from __future__ import annotations


class McpError(Exception):
    """Base class for every MCP level failure."""


class ProtocolVersionError(McpError):
    """The server picked a protocol version this host cannot speak."""


class NotInitializedError(McpError):
    """A request was attempted before the initialize handshake completed."""


class ServerConnectionError(McpError):
    """The underlying transport died while a session was active."""
