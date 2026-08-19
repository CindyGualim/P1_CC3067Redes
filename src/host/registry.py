"""Registry of MCP servers the host talks to.

The chatbot is the *host*: it owns one client per server and presents their
tools to the LLM as a single flat catalogue. Names are qualified as
``<server>__<tool>`` so two servers can expose a ``search`` tool without
colliding, and so the log shows which server answered.

Servers are declared in ``config/servers.json``; adding the official Filesystem
and Git servers is a matter of adding entries there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.mcp.client import MCPClient
from core.mcp.protocol_log import ProtocolLogger
from core.mcp.types import CallToolResult, Tool
from core.transport.stdio import StdioTransport
from host.llm.base import ToolSpec

logger = logging.getLogger(__name__)

QUALIFIER = "__"


@dataclass
class ServerConfig:
    name: str
    command: List[str]
    description: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    enabled: bool = True


@dataclass
class RegisteredTool:
    """One tool, bound to the server that provides it."""

    server: str
    tool: Tool

    @property
    def qualified_name(self) -> str:
        return f"{self.server}{QUALIFIER}{self.tool.name}"

    @property
    def requires_approval(self) -> bool:
        """Ask the user before running anything that is not read-only.

        The flag comes from the server's own ``annotations.readOnlyHint``, so
        the host does not need a hardcoded list of dangerous tool names: a new
        server that writes is protected the moment it declares itself.
        """
        annotations = self.tool.annotations or {}
        return not annotations.get("readOnlyHint", False)

    def to_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.qualified_name,
            description=self.tool.description or "",
            input_schema=self.tool.inputSchema or {"type": "object", "properties": {}},
        )


def load_server_configs(
    path: Path, project_root: Path, workspace: Optional[Path] = None
) -> List[ServerConfig]:
    """Read the declarations, expanding the placeholders in the commands.

    ``${PYTHON}``, ``${PROJECT_ROOT}`` and ``${WORKSPACE}`` keep the file
    portable: the same config works on another machine without editing absolute
    paths. ``${WORKSPACE}`` is what bounds the official Filesystem server, so it
    is resolved to a real absolute path rather than pasted as written.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    replacements = {
        "${PYTHON}": sys.executable,
        "${PROJECT_ROOT}": str(project_root),
        "${WORKSPACE}": str((workspace or project_root / "workspace").resolve()),
    }

    configs: List[ServerConfig] = []
    for entry in raw.get("servers", []):
        command = resolve_executable(
            [_expand(part, replacements) for part in entry["command"]]
        )
        configs.append(
            ServerConfig(
                name=entry["name"],
                command=command,
                description=entry.get("description", ""),
                env={
                    key: _expand(value, replacements)
                    for key, value in (entry.get("env") or {}).items()
                },
                cwd=_expand(entry["cwd"], replacements) if entry.get("cwd") else None,
                enabled=entry.get("enabled", True),
            )
        )
    return configs


def _expand(value: str, replacements: Dict[str, str]) -> str:
    for placeholder, actual in replacements.items():
        value = value.replace(placeholder, actual)
    return os.path.expandvars(value)


def resolve_executable(command: List[str]) -> List[str]:
    """Resolve the program of a command through PATH.

    Needed for the official servers: ``npx`` on Windows is really ``npx.cmd``,
    and ``create_subprocess_exec`` does not consult PATHEXT the way a shell
    does, so launching it by bare name fails with WinError 2.
    """
    if not command:
        return command

    program = command[0]
    if Path(program).exists():
        return command

    found = shutil.which(program)
    if found is None and sys.platform == "win32":
        for extension in (".cmd", ".exe", ".bat"):
            found = shutil.which(program + extension)
            if found:
                break
    return [found or program, *command[1:]]


def executable_missing(command: List[str]) -> bool:
    return not command or (
        not Path(command[0]).exists() and shutil.which(command[0]) is None
    )


class ServerRegistry:
    def __init__(
        self,
        configs: List[ServerConfig],
        *,
        protocol_logger: Optional[ProtocolLogger] = None,
        request_timeout: float = 60.0,
    ) -> None:
        self.configs = [config for config in configs if config.enabled]
        self.protocol_logger = protocol_logger
        self.request_timeout = request_timeout
        self.clients: Dict[str, MCPClient] = {}
        self.tools: List[RegisteredTool] = []
        self.failures: Dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def connect_all(self) -> None:
        """Open every configured server, in parallel, tolerating failures.

        One unavailable server (a missing npx, say) must not take the chatbot
        down: it is recorded in ``failures`` and the rest keep working.
        """
        results = await asyncio.gather(
            *(self._connect(config) for config in self.configs),
            return_exceptions=True,
        )
        for config, outcome in zip(self.configs, results):
            if isinstance(outcome, Exception):
                self.failures[config.name] = str(outcome)
                logger.error("Could not start server '%s': %s", config.name, outcome)

        self.tools = [
            RegisteredTool(server=name, tool=tool)
            for name, client in self.clients.items()
            for tool in client.tools
        ]
        logger.info(
            "Registry ready: %d server(s), %d tool(s)", len(self.clients), len(self.tools)
        )

    async def _connect(self, config: ServerConfig) -> None:
        if executable_missing(config.command):
            raise FileNotFoundError(
                f"No se encontro el ejecutable '{config.command[0]}'. "
                "Instale la dependencia o desactive el servidor en config/servers.json."
            )
        transport = StdioTransport(
            config.command, name=config.name, env=config.env, cwd=config.cwd
        )
        client = MCPClient(
            transport,
            protocol_logger=self.protocol_logger,
            request_timeout=self.request_timeout,
        )
        await client.connect()
        await client.list_tools()
        self.clients[config.name] = client

    async def close_all(self) -> None:
        await asyncio.gather(
            *(client.close() for client in self.clients.values()), return_exceptions=True
        )
        self.clients.clear()

    # ------------------------------------------------------------------ #
    # Catalogue
    # ------------------------------------------------------------------ #
    def tool_specs(self) -> List[ToolSpec]:
        return [tool.to_spec() for tool in self.tools]

    def find(self, qualified_name: str) -> Optional[RegisteredTool]:
        for tool in self.tools:
            if tool.qualified_name == qualified_name:
                return tool
        return None

    def instructions(self) -> Dict[str, str]:
        """Per-server ``instructions`` from the handshake, for the system prompt."""
        return {
            name: client.initialize_result.instructions
            for name, client in self.clients.items()
            if client.initialize_result and client.initialize_result.instructions
        }

    def describe(self) -> List[Dict[str, Any]]:
        """Snapshot for the UI header and the /servers command."""
        rows: List[Dict[str, Any]] = []
        for name, client in self.clients.items():
            info = client.server_info
            rows.append(
                {
                    "name": name,
                    "server": info.name if info else "?",
                    "version": info.version if info else "?",
                    "protocol": client.protocol_version,
                    "tools": [tool.name for tool in client.tools],
                    "status": "connected",
                }
            )
        for name, reason in self.failures.items():
            rows.append({"name": name, "status": "failed", "reason": reason, "tools": []})
        return rows

    # ------------------------------------------------------------------ #
    # Invocation
    # ------------------------------------------------------------------ #
    async def call(self, qualified_name: str, arguments: Dict[str, Any]) -> CallToolResult:
        """Route a qualified tool name to its server."""
        registered = self.find(qualified_name)
        if registered is None:
            available = ", ".join(tool.qualified_name for tool in self.tools)
            return CallToolResult(
                content=[
                    {
                        "type": "text",
                        "text": (
                            f"La herramienta '{qualified_name}' no existe. "
                            f"Disponibles: {available}"
                        ),
                    }
                ],
                isError=True,
            )

        client = self.clients[registered.server]
        return await client.call_tool(registered.tool.name, arguments)
