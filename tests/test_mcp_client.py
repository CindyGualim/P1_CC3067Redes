"""End-to-end tests of the MCP session against the mock server fixture.

They spawn a real subprocess and speak the real protocol over stdio, so they
cover the handshake, the id correlation and the protocol log in one go.
"""

import sys
from pathlib import Path

import pytest

from core.mcp.client import MCPClient
from core.mcp.protocol_log import ProtocolLogger
from core.mcp.types import Method
from core.transport.stdio import StdioTransport

MOCK_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"


@pytest.fixture
def protocol_logger(tmp_path):
    return ProtocolLogger(tmp_path / "logs", filename="test_protocol.jsonl")


@pytest.fixture
async def client(protocol_logger):
    transport = StdioTransport(
        [sys.executable, str(MOCK_SERVER)], name="mock", env={"PYTHONIOENCODING": "utf-8"}
    )
    session = MCPClient(transport, protocol_logger=protocol_logger, request_timeout=15)
    await session.connect()
    yield session
    await session.close()


async def test_handshake_negotiates_protocol_version(client):
    assert client.protocol_version == "2025-11-25"
    assert client.server_info.name == "mock-server"
    assert client.initialize_result.capabilities.tools is not None


async def test_list_tools_returns_catalogue(client):
    tools = await client.list_tools()
    assert [tool.name for tool in tools] == ["echo"]
    assert tools[0].inputSchema["required"] == ["text"]


async def test_call_tool_returns_content(client):
    result = await client.call_tool("echo", {"text": "ibuprofeno 400 mg"})
    assert result.isError is False
    assert result.as_text() == "ibuprofeno 400 mg"


async def test_unknown_tool_is_reported_as_tool_error(client):
    """A JSON-RPC error must not crash the turn: the LLM has to read it."""
    result = await client.call_tool("does_not_exist", {})
    assert result.isError is True
    assert "MCP error" in result.as_text()


async def test_ping(client):
    assert await client.ping() is True


async def test_protocol_log_records_both_directions(client, protocol_logger):
    await client.list_tools()

    methods = [entry.method for entry in protocol_logger.snapshot()]
    assert Method.INITIALIZE in methods
    assert Method.INITIALIZED in methods
    assert Method.TOOLS_LIST in methods

    kinds = {entry.kind for entry in protocol_logger.snapshot()}
    assert {"request", "response", "notification"} <= kinds

    # Responses are matched to their request, so they carry a round-trip time.
    responses = [e for e in protocol_logger.snapshot() if e.kind == "response"]
    assert all(entry.elapsed_ms is not None for entry in responses)
    assert protocol_logger.path.exists()


async def test_concurrent_calls_are_matched_by_id(client):
    """Ids, not ordering, are what pairs a response with its request."""
    import asyncio

    results = await asyncio.gather(
        *(client.call_tool("echo", {"text": f"call-{i}"}) for i in range(5))
    )
    assert [result.as_text() for result in results] == [f"call-{i}" for i in range(5)]
