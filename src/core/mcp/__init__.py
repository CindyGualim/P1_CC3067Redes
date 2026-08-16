"""Hand-written Model Context Protocol layer (no MCP SDK is used)."""

from core.mcp.client import MCPClient
from core.mcp.errors import (
    McpError,
    NotInitializedError,
    ProtocolVersionError,
    ServerConnectionError,
)
from core.mcp.protocol_log import ProtocolEntry, ProtocolLogger
from core.mcp.types import CallToolResult, Implementation, Method, Tool

__all__ = [
    "MCPClient",
    "McpError",
    "NotInitializedError",
    "ProtocolVersionError",
    "ServerConnectionError",
    "ProtocolEntry",
    "ProtocolLogger",
    "CallToolResult",
    "Implementation",
    "Method",
    "Tool",
]
