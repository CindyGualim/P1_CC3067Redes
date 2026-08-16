"""Data model of the MCP messages this project exchanges.

Only the subset the chatbot actually needs is modelled (lifecycle + tools).
Resources, prompts and sampling are declared but not consumed, so the host
advertises no capability for them during the handshake.

Reference: https://modelcontextprotocol.io/specification/2025-11-25
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

#: Versions this host can speak, newest first. ``initialize`` offers the first
#: one and the server answers with the version it picked; if that version is in
#: this list the session continues, otherwise it is aborted.
LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = [
    LATEST_PROTOCOL_VERSION,
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
]


# --------------------------------------------------------------------------- #
# Method names, kept as constants so typos fail at import time, not at runtime.
# --------------------------------------------------------------------------- #
class Method:
    INITIALIZE = "initialize"
    INITIALIZED = "notifications/initialized"
    PING = "ping"
    TOOLS_LIST = "tools/list"
    TOOLS_CALL = "tools/call"
    TOOLS_LIST_CHANGED = "notifications/tools/list_changed"
    CANCELLED = "notifications/cancelled"
    LOG_MESSAGE = "notifications/message"
    PROGRESS = "notifications/progress"


class Implementation(BaseModel):
    """Identity of either side of the connection."""

    name: str
    version: str
    title: Optional[str] = None


class ClientCapabilities(BaseModel):
    """What the host offers to the server. We consume tools, we expose nothing."""

    model_config = ConfigDict(extra="allow")

    roots: Optional[Dict[str, Any]] = None
    sampling: Optional[Dict[str, Any]] = None
    elicitation: Optional[Dict[str, Any]] = None


class ServerCapabilities(BaseModel):
    """What the server declares it supports. Presence of the key is the flag."""

    model_config = ConfigDict(extra="allow")

    tools: Optional[Dict[str, Any]] = None
    resources: Optional[Dict[str, Any]] = None
    prompts: Optional[Dict[str, Any]] = None
    logging: Optional[Dict[str, Any]] = None
    completions: Optional[Dict[str, Any]] = None


class InitializeResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    protocolVersion: str
    capabilities: ServerCapabilities = Field(default_factory=ServerCapabilities)
    serverInfo: Implementation
    instructions: Optional[str] = None


class Tool(BaseModel):
    """A single callable exposed by a server.

    ``inputSchema`` is a JSON Schema object; the host translates it into the
    function declarations the LLM understands, which is what makes the tools
    portable across providers.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    description: Optional[str] = None
    title: Optional[str] = None
    inputSchema: Dict[str, Any] = Field(default_factory=dict)
    outputSchema: Optional[Dict[str, Any]] = None
    annotations: Optional[Dict[str, Any]] = None


class ListToolsResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    tools: List[Tool] = Field(default_factory=list)
    nextCursor: Optional[str] = None


class TextContent(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["text"] = "text"
    text: str


class CallToolResult(BaseModel):
    """Outcome of ``tools/call``.

    Note the distinction that trips people up: a *tool* failure is reported with
    ``isError: true`` inside a successful JSON-RPC response, because the LLM is
    supposed to read it and react. A JSON-RPC error object means the call itself
    was invalid (unknown tool, bad params).
    """

    model_config = ConfigDict(extra="allow")

    content: List[Dict[str, Any]] = Field(default_factory=list)
    structuredContent: Optional[Dict[str, Any]] = None
    isError: bool = False

    def as_text(self) -> str:
        """Flatten the content blocks into the text handed back to the LLM."""
        parts: List[str] = []
        for block in self.content:
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                parts.append(f"[{block.get('type', 'unknown')} content]")
        return "\n".join(parts)
