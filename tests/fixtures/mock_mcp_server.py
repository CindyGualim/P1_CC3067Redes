"""Minimal MCP server used to exercise the client in tests.

It is deliberately tiny and dependency-free: it reads one JSON object per line
from stdin and writes one per line to stdout, which is exactly the stdio framing
the real servers use. The pharmacy server of the next commit follows the same
shape, this one just answers with an echo.
"""

from __future__ import annotations

import json
import sys

PROTOCOL_VERSION = "2025-11-25"

TOOLS = [
    {
        "name": "echo",
        "description": "Return the text it receives, for connectivity checks.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }
]


def write(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def success(request_id, result) -> None:
    write({"jsonrpc": "2.0", "id": request_id, "result": result})


def error(request_id, code: int, message: str) -> None:
    write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def handle(request: dict) -> None:
    method = request.get("method")
    request_id = request.get("id")

    # Notifications carry no id and must never be answered.
    if request_id is None:
        return

    if method == "initialize":
        success(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "mock-server", "version": "1.0.0"},
            },
        )
    elif method == "ping":
        success(request_id, {})
    elif method == "tools/list":
        success(request_id, {"tools": TOOLS})
    elif method == "tools/call":
        params = request.get("params") or {}
        if params.get("name") != "echo":
            error(request_id, -32602, f"Unknown tool: {params.get('name')}")
            return
        text = (params.get("arguments") or {}).get("text", "")
        success(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
    else:
        error(request_id, -32601, f"Method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except json.JSONDecodeError:
            error(None, -32700, "Parse error")


if __name__ == "__main__":
    main()
