"""Manual smoke test of the MCP core (requirement #3, protocol log).

Runs a full session against the mock server and prints every JSON-RPC message
that crossed the boundary, classified as synchronization / request / response,
which is the same breakdown the Wireshark analysis will need later.

    python scripts/demo_protocol.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.logging_setup import setup_logging  # noqa: E402
from core.mcp.client import MCPClient  # noqa: E402
from core.mcp.protocol_log import ProtocolLogger  # noqa: E402
from core.transport.stdio import StdioTransport, configure_event_loop  # noqa: E402

MOCK_SERVER = PROJECT_ROOT / "tests" / "fixtures" / "mock_mcp_server.py"

#: The lifecycle methods are the "synchronization" messages of the protocol.
SYNC_METHODS = {"initialize", "notifications/initialized", "ping"}


async def main() -> None:
    setup_logging(PROJECT_ROOT / "logs", level="INFO", console=True)
    protocol_logger = ProtocolLogger(PROJECT_ROOT / "logs")

    transport = StdioTransport(
        [sys.executable, str(MOCK_SERVER)],
        name="mock",
        env={"PYTHONIOENCODING": "utf-8"},
    )
    async with MCPClient(transport, protocol_logger=protocol_logger) as client:
        tools = await client.list_tools()
        print(f"\nTools advertised: {[tool.name for tool in tools]}")
        result = await client.call_tool("echo", {"text": "Hola desde el host MCP"})
        print(f"Tool result     : {result.as_text()}")
        print(f"Unknown tool    : {(await client.call_tool('nope', {})).as_text()}\n")

    print("=" * 78)
    print("MCP PROTOCOL LOG")
    print("=" * 78)
    for entry in protocol_logger.snapshot():
        role = "SYNC" if entry.method in SYNC_METHODS else entry.kind.upper()
        print(f"{entry.timestamp}  {role:<13} {entry.summary()}")
        print(f"    {json.dumps(entry.payload, ensure_ascii=False)[:160]}")

    print("\nCounters:", protocol_logger.stats())
    print("Saved to :", protocol_logger.path)


if __name__ == "__main__":
    configure_event_loop()
    asyncio.run(main())
