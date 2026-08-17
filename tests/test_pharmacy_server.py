"""Tests for the pharmacy MCP server.

Split in three levels, cheapest first:

* the SQLite layer and its business rules,
* the JSON-RPC dispatch of ``MCPServer`` in process,
* one end-to-end session where the real host client spawns the real server.
"""

from datetime import date, timedelta
from pathlib import Path

import pytest

from core.jsonrpc import ErrorCode
from core.mcp.types import Method
from servers.pharmacy.database import PharmacyDatabase, PharmacyError
from servers.pharmacy.tools import build_server

SEED = Path(__file__).resolve().parents[1] / "data" / "pharmacy_seed.json"


def _shift_expiry(db: PharmacyDatabase, folio: str, days: int) -> None:
    """Move a prescription's expiry relative to today, so tests never rot."""
    target = (date.today() + timedelta(days=days)).isoformat()
    with db.conn:
        db.conn.execute(
            "UPDATE prescriptions SET expires_at = ? WHERE folio = ?", (target, folio)
        )


@pytest.fixture
def db(tmp_path):
    database = PharmacyDatabase(tmp_path / "pharmacy.db", SEED)
    yield database
    database.close()


@pytest.fixture
def server(db):
    mcp = build_server(db)
    # Every test starts from an initialized session.
    mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": Method.INITIALIZE,
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"},
            },
        }
    )
    mcp.handle_message({"jsonrpc": "2.0", "method": Method.INITIALIZED})
    return mcp


def call(server, name, arguments=None, request_id=99):
    return server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": Method.TOOLS_CALL,
            "params": {"name": name, "arguments": arguments or {}},
        }
    )


# --------------------------------------------------------------------------- #
# Database rules
# --------------------------------------------------------------------------- #
def test_seed_loads_once(db, tmp_path):
    assert len(db.list_branches()) == 3
    assert db.tax_rate == 0.12

    # Re-opening the same file must not duplicate the catalogue.
    again = PharmacyDatabase(tmp_path / "pharmacy.db", SEED)
    assert (
        again.conn.execute("SELECT COUNT(*) AS n FROM medicines").fetchone()["n"] == 16
    )
    again.close()


def test_search_by_symptom_ignores_accents(db):
    results = db.search_medicines(symptom="congestión nasal")
    assert {med["sku"] for med in results} >= {"MED-003", "MED-015"}


def test_search_can_filter_over_the_counter(db):
    results = db.search_medicines(symptom="infeccion bacteriana", prescription_filter="otc_only")
    assert results == []


def test_order_of_otc_medicine_decrements_stock(db):
    before = db.get_inventory(sku="MED-001", branch_id="SUC-01")[0]["stock"]
    order = db.create_order(
        branch_id="SUC-01",
        customer_name="Cliente de prueba",
        items=[{"sku": "MED-001", "quantity": 2}],
    )
    after = db.get_inventory(sku="MED-001", branch_id="SUC-01")[0]["stock"]

    assert after == before - 2
    assert order["subtotal"] == 37.00
    assert order["tax"] == pytest.approx(4.44)
    assert order["total"] == pytest.approx(41.44)


def test_prescription_medicine_requires_a_folio(db):
    with pytest.raises(PharmacyError, match="requieren receta"):
        db.create_order(
            branch_id="SUC-01",
            customer_name="Ana Lucia Morales",
            items=[{"sku": "MED-005", "quantity": 1}],
        )


def test_expired_prescription_is_rejected(db):
    _shift_expiry(db, "RX-2026-0001", -1)
    with pytest.raises(PharmacyError, match="vencio"):
        db.create_order(
            branch_id="SUC-01",
            customer_name="Ana Lucia Morales",
            items=[{"sku": "MED-005", "quantity": 1}],
            prescription_folio="RX-2026-0001",
        )


def test_valid_prescription_dispenses_and_closes_it(db):
    _shift_expiry(db, "RX-2026-0001", 30)
    order = db.create_order(
        branch_id="SUC-01",
        customer_name="Ana Lucia Morales",
        customer_id="2547891230101",
        items=[{"sku": "MED-005", "quantity": 1}, {"sku": "MED-010", "quantity": 1}],
        prescription_folio="RX-2026-0001",
    )
    assert order["prescription_folio"] == "RX-2026-0001"

    prescription = db.get_prescription("RX-2026-0001")
    assert prescription["status"] == "dispensed"
    assert all(item["quantity_remaining"] == 0 for item in prescription["items"])


def test_prescription_does_not_cover_extra_quantity(db):
    _shift_expiry(db, "RX-2026-0005", 30)
    # Three prescribed, one already dispensed: only two remain.
    with pytest.raises(PharmacyError, match="pendientes"):
        db.create_order(
            branch_id="SUC-01",
            customer_name="Ana Lucia Morales",
            items=[{"sku": "MED-009", "quantity": 3}],
            prescription_folio="RX-2026-0005",
        )


def test_patient_id_must_match_the_prescription(db):
    _shift_expiry(db, "RX-2026-0001", 30)
    result = db.validate_prescription("RX-2026-0001", patient_id="0000000000000")
    assert result["valid"] is False
    assert any("identificacion" in problem for problem in result["problems"])


def test_out_of_stock_suggests_another_branch(db):
    # MED-005 has zero stock in SUC-02 but plenty in the other two.
    with pytest.raises(PharmacyError, match="Disponible en"):
        db.create_order(
            branch_id="SUC-02",
            customer_name="Cliente",
            items=[{"sku": "MED-005", "quantity": 1}],
            prescription_folio="RX-2026-0001",
        )


def test_rejected_order_leaves_no_trace(db):
    before = db.get_inventory(sku="MED-001", branch_id="SUC-01")[0]["stock"]
    with pytest.raises(PharmacyError):
        db.create_order(
            branch_id="SUC-01",
            customer_name="Cliente",
            # The second line is unsellable, so the whole order must fail.
            items=[{"sku": "MED-001", "quantity": 1}, {"sku": "MED-005", "quantity": 1}],
        )
    assert db.get_inventory(sku="MED-001", branch_id="SUC-01")[0]["stock"] == before


# --------------------------------------------------------------------------- #
# JSON-RPC dispatch
# --------------------------------------------------------------------------- #
def test_tools_list_exposes_the_catalogue(server):
    response = server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": Method.TOOLS_LIST}
    )
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert names == [
        "list_branches",
        "search_medicines",
        "get_medicine_details",
        "check_inventory",
        "verify_prescription",
        "create_purchase_order",
        "get_order",
    ]
    # Every tool must document its arguments; that schema is what the LLM sees.
    assert all(tool["inputSchema"]["type"] == "object" for tool in response["result"]["tools"])


def test_initialize_negotiates_and_reports_capabilities(db):
    mcp = build_server(db)
    response = mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": Method.INITIALIZE,
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        }
    )
    result = response["result"]
    assert result["protocolVersion"] == "2024-11-05"  # the client's choice is honoured
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == "pharmacy-mcp-server"
    assert "instructions" in result


def test_notifications_are_never_answered(server):
    assert server.handle_message({"jsonrpc": "2.0", "method": Method.INITIALIZED}) is None


def test_tools_before_initialize_are_refused(db):
    mcp = build_server(db)
    response = mcp.handle_message({"jsonrpc": "2.0", "id": 1, "method": Method.TOOLS_LIST})
    assert response["error"]["code"] == ErrorCode.NOT_INITIALIZED


def test_unknown_method_returns_method_not_found(server):
    response = server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    assert response["error"]["code"] == ErrorCode.METHOD_NOT_FOUND


def test_missing_required_argument_is_a_protocol_error(server):
    response = call(server, "get_medicine_details", {})
    assert response["error"]["code"] == ErrorCode.INVALID_PARAMS
    assert "sku" in response["error"]["message"]


def test_wrong_argument_type_is_a_protocol_error(server):
    response = call(server, "search_medicines", {"limit": "muchos"})
    assert response["error"]["code"] == ErrorCode.INVALID_PARAMS


def test_enum_is_enforced(server):
    response = call(server, "search_medicines", {"symptom": "tos", "prescription_filter": "gratis"})
    assert response["error"]["code"] == ErrorCode.INVALID_PARAMS


def test_nested_array_items_are_validated(server):
    response = call(
        server,
        "create_purchase_order",
        {"branch_id": "SUC-01", "customer_name": "X", "items": [{"sku": "MED-001"}]},
    )
    assert response["error"]["code"] == ErrorCode.INVALID_PARAMS


def test_business_failure_is_a_tool_error_not_a_protocol_error(server):
    """isError is how the LLM learns it must ask for the prescription."""
    response = call(
        server,
        "create_purchase_order",
        {
            "branch_id": "SUC-01",
            "customer_name": "Cliente",
            "items": [{"sku": "MED-005", "quantity": 1}],
        },
    )
    assert "error" not in response
    assert response["result"]["isError"] is True
    assert "receta" in response["result"]["content"][0]["text"]


def test_successful_call_returns_text_and_structured_content(server):
    response = call(server, "search_medicines", {"symptom": "tos"})
    result = response["result"]
    assert result["isError"] is False
    assert "MED-010" in result["content"][0]["text"]
    assert result["structuredContent"]["count"] >= 1


def test_unknown_tool_is_invalid_params(server):
    response = call(server, "buy_everything", {})
    assert response["error"]["code"] == ErrorCode.INVALID_PARAMS
