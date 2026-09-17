"""Remote MCP server and client over real local HTTP sockets."""

from __future__ import annotations

import asyncio
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from core.mcp.client import MCPClient
from core.transport.http import HttpTransport
from core.transport.http_server import create_http_server
from servers.pharmacy.database import PharmacyDatabase
from servers.pharmacy.tools import build_server


@pytest.fixture
def remote_pharmacy(tmp_path):
    from pathlib import Path

    # pytest's tmp path is outside the repository, so resolve the seed from here.
    seed = Path(__file__).resolve().parents[1] / "data" / "pharmacy_seed.json"
    database = PharmacyDatabase(tmp_path / "remote.db", seed)
    httpd = create_http_server(
        build_server(database), "127.0.0.1", 0, auth_token="test-secret"
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", "test-secret"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)
    database.close()


def test_health_endpoint_needs_no_credentials(remote_pharmacy):
    base_url, _ = remote_pharmacy
    with urlopen(f"{base_url}/health", timeout=5) as response:
        payload = json.load(response)
    assert response.status == 200
    assert payload == {
        "status": "ok",
        "server": "pharmacy-mcp-server",
        "version": "1.0.0",
    }


def test_mcp_endpoint_rejects_missing_bearer_token(remote_pharmacy):
    base_url, _ = remote_pharmacy
    request = Request(
        f"{base_url}/mcp",
        data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(HTTPError) as captured:
        urlopen(request, timeout=5)
    assert captured.value.code == 401


async def test_remote_client_completes_handshake_and_calls_tool(remote_pharmacy):
    base_url, token = remote_pharmacy
    transport = HttpTransport(
        f"{base_url}/mcp",
        name="pharmacy_remote",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    client = MCPClient(transport, request_timeout=5)
    try:
        await client.connect()
        tools = await client.list_tools()
        assert len(tools) == 7
        assert client.protocol_version == "2025-11-25"

        result = await client.call_tool("search_medicines", {"symptom": "tos"})
        assert result.isError is False
        assert "MED-010" in result.as_text()
    finally:
        await client.close()


async def test_remote_calls_are_correlated_by_jsonrpc_id(remote_pharmacy):
    base_url, token = remote_pharmacy
    transport = HttpTransport(
        f"{base_url}/mcp",
        name="pharmacy_remote",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    client = MCPClient(transport, request_timeout=5)
    try:
        await client.connect()
        results = await asyncio.gather(
            client.call_tool("get_medicine_details", {"sku": "MED-001"}),
            client.call_tool("get_medicine_details", {"sku": "MED-010"}),
        )
        assert [result.structuredContent["sku"] for result in results] == [
            "MED-001",
            "MED-010",
        ]
    finally:
        await client.close()
