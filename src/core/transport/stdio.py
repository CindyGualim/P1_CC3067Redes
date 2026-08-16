"""stdio transport: runs an MCP server as a child process.

Framing is one JSON object per line on stdout, exactly as the MCP stdio
specification mandates. The child's stderr is *not* part of the protocol, so it
is drained separately into the log file (official servers print banners and
warnings there, and a full stderr pipe would deadlock the child).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional

from core.transport.base import Transport, TransportError

logger = logging.getLogger(__name__)


class StdioTransport(Transport):
    def __init__(
        self,
        command: List[str],
        *,
        name: str = "stdio",
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[str] = None,
    ) -> None:
        self.command = command
        self.name = name
        self.cwd = cwd
        # Inherit the parent environment so PATH/APPDATA stay available, then
        # layer the server specific variables on top.
        self.env: Dict[str, str] = {**os.environ, **(env or {})}
        self.process: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        logger.debug("Starting MCP server '%s': %s", self.name, " ".join(self.command))
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            cwd=self.cwd,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        """Forward the child's stderr to our logger, never to the protocol."""
        assert self.process is not None and self.process.stderr is not None
        async for raw in self.process.stderr:
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.debug("[%s stderr] %s", self.name, text)

    async def send(self, message: Dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise TransportError(f"Transport '{self.name}' is not started")
        # ensure_ascii=False keeps accented Spanish text readable on the wire;
        # the newline is the frame delimiter.
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        self.process.stdin.write((payload + "\n").encode("utf-8"))
        await self.process.stdin.drain()

    async def receive(self) -> AsyncIterator[Dict[str, Any]]:
        if not self.process or not self.process.stdout:
            raise TransportError(f"Transport '{self.name}' is not started")
        async for raw in self.process.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # Some servers leak plain text into stdout. Skipping keeps the
                # session alive instead of killing it over a stray banner.
                logger.warning("[%s] Non-JSON line on stdout: %r", self.name, line[:300])

    async def close(self) -> None:
        if not self.process:
            return
        # Closing stdin is the graceful shutdown signal for a stdio server.
        try:
            if self.process.stdin and not self.process.stdin.is_closing():
                self.process.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

        try:
            await asyncio.wait_for(self.process.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("[%s] Server did not exit, terminating", self.name)
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.process.kill()

        if self._stderr_task:
            self._stderr_task.cancel()
        self.process = None
        logger.debug("[%s] Transport closed", self.name)


def configure_event_loop() -> None:
    """Windows needs the Proactor loop to spawn subprocesses with pipes."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
