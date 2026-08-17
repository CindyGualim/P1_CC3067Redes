"""End-to-end: the host client drives the pharmacy server as a real subprocess.

This is the scenario the project asks to demonstrate — symptom, stock,
prescription, purchase order — but driven by code instead of by the LLM, so it
can run in CI and fail loudly if the protocol regresses.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

from core.mcp.client import MCPClient
from core.mcp.protocol_log import ProtocolLogger
from core.transport.stdio import StdioTransport

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_MODULE = PROJECT_ROOT / "src" / "servers" / "pharmacy" / "__main__.py"


@pytest.fixture
async def pharmacy(tmp_path):
    """Spawn the server with a throwaway database, exactly as the host does."""
    transport = StdioTransport(
        [sys.executable, str(SERVER_MODULE)],
        name="pharmacy",
        env={
            "PHARMACY_DB": str(tmp_path / "e2e.db"),
            "PHARMACY_SEED": str(PROJECT_ROOT / "data" / "pharmacy_seed.json"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        },
    )
    logger = ProtocolLogger(tmp_path / "logs", filename="e2e.jsonl")
    client = MCPClient(transport, protocol_logger=logger, request_timeout=30)
    await client.connect()
    yield client, logger
    await client.close()


async def test_handshake_exposes_pharmacy_tools(pharmacy):
    client, _ = pharmacy
    assert client.server_info.name == "pharmacy-mcp-server"
    assert "farmacias" in client.initialize_result.instructions

    tools = await client.list_tools()
    assert len(tools) == 7
    order_tool = next(tool for tool in tools if tool.name == "create_purchase_order")
    assert order_tool.inputSchema["required"] == ["branch_id", "customer_name", "items"]


async def test_full_purchase_scenario(pharmacy):
    """Symptom -> details -> stock -> order, over the wire."""
    client, protocol_logger = pharmacy

    found = await client.call_tool("search_medicines", {"symptom": "dolor de cabeza"})
    assert found.isError is False
    assert "MED-001" in found.as_text()

    details = await client.call_tool("get_medicine_details", {"sku": "MED-001"})
    assert "Acetaminofen" in details.as_text()
    assert details.structuredContent["requires_prescription"] is False

    stock = await client.call_tool("check_inventory", {"sku": "MED-001", "branch_id": "SUC-01"})
    available = stock.structuredContent["total_stock"]

    order = await client.call_tool(
        "create_purchase_order",
        {
            "branch_id": "SUC-01",
            "customer_name": "Ana Lucia Morales",
            "items": [{"sku": "MED-001", "quantity": 3}],
        },
    )
    assert order.isError is False
    assert order.structuredContent["total"] == pytest.approx(62.16)

    after = await client.call_tool("check_inventory", {"sku": "MED-001", "branch_id": "SUC-01"})
    assert after.structuredContent["total_stock"] == available - 3

    # The whole exchange was recorded, in both directions.
    methods = [entry.method for entry in protocol_logger.snapshot()]
    assert methods.count("tools/call") == 5


async def test_controlled_medicine_needs_a_valid_prescription(pharmacy):
    client, _ = pharmacy

    refused = await client.call_tool(
        "create_purchase_order",
        {
            "branch_id": "SUC-01",
            "customer_name": "Luis Fernando Ordonez",
            "items": [{"sku": "MED-013", "quantity": 1}],
        },
    )
    assert refused.isError is True
    assert "receta" in refused.as_text().lower()

    check = await client.call_tool("verify_prescription", {"folio": "RX-2026-0004"})
    expires = date.fromisoformat(check.structuredContent["expires_at"])
    if expires < date.today():
        pytest.skip("Seed prescription RX-2026-0004 expired; covered by the unit tests")

    granted = await client.call_tool(
        "create_purchase_order",
        {
            "branch_id": "SUC-01",
            "customer_name": "Luis Fernando Ordonez",
            "customer_id": "2233445560101",
            "items": [{"sku": "MED-013", "quantity": 1}],
            "prescription_folio": "RX-2026-0004",
        },
    )
    assert granted.isError is False
    assert granted.structuredContent["prescription_folio"] == "RX-2026-0004"


async def test_unknown_folio_is_a_readable_tool_error(pharmacy):
    client, _ = pharmacy
    result = await client.call_tool("verify_prescription", {"folio": "RX-0000-0000"})
    assert result.isError is True
    assert "No se encontro" in result.as_text()


async def test_invalid_arguments_come_back_as_protocol_errors(pharmacy):
    """Schema violations never reach the handler; the server rejects them."""
    client, _ = pharmacy
    result = await client.call_tool("create_purchase_order", {"branch_id": "SUC-01"})
    assert result.isError is True
    assert "-32602" in result.as_text()

