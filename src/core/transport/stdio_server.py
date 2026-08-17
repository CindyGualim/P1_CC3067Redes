"""stdin/stdout loop that turns an :class:`MCPServer` into a runnable process.

Two rules matter here and both are easy to get wrong:

* **stdout belongs to the protocol.** A stray ``print`` corrupts the stream, so
  every diagnostic goes to stderr, which the host drains into its log file.
* **One JSON object per line.** No Content-Length framing in stdio MCP.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import TextIO

from core.jsonrpc import ErrorCode, build_error
from core.mcp.server import MCPServer, dumps

logger = logging.getLogger(__name__)


def configure_stderr_logging(level: str = "INFO") -> None:
    """Send this process' logs to stderr, never to stdout."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s | %(name)s | %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def serve_stdio(
    server: MCPServer,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> None:
    """Block reading messages until stdin is closed by the host."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    logger.info(
        "MCP server '%s' listening on stdio with tools: %s",
        server.info.name,
        ", ".join(server.tool_names),
    )

    for line in stdin:
        line = line.strip()
        if not line:
            continue

        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            # Malformed JSON: the id is unknown, so the reply carries null.
            _write(stdout, build_error(None, ErrorCode.PARSE_ERROR, "Parse error").to_wire())
            continue

        response = server.handle_message(payload)
        if response is not None:
            _write(stdout, response)

    logger.info("stdin closed, shutting down")


def _write(stdout: TextIO, payload: dict) -> None:
    stdout.write(dumps(payload) + "\n")
    stdout.flush()
