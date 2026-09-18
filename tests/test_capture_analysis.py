"""The Wireshark analysis must label JSON-RPC messages the way item 7 asks.

Only the pure helpers are exercised here: capturing needs Wireshark, Npcap and
administrator rights, so ``scripts/capture_mcp_session.py`` is not run by the
suite.  The classification rules, however, are what the report is built on, so
they are pinned.
"""

from __future__ import annotations

import binascii
import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _load_analyzer():
    """scripts/ is not a package, so load the module straight from its path."""
    path = PROJECT_ROOT / "scripts" / "analyze_capture.py"
    spec = importlib.util.spec_from_file_location("analyze_capture", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


analyzer = _load_analyzer()


def hexed(payload: dict) -> str:
    """Encode a body the way tshark renders the http.file_data column."""
    return binascii.hexlify(json.dumps(payload).encode("utf-8")).decode("ascii")


@pytest.mark.parametrize(
    "message, expected_category",
    [
        ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, "SINCRONIZACION"),
        ({"jsonrpc": "2.0", "method": "notifications/initialized"}, "SINCRONIZACION"),
        ({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}}, "SINCRONIZACION"),
        ({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "SOLICITUD"),
        ({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_order"}}, "SOLICITUD"),
        ({"jsonrpc": "2.0", "method": "notifications/cancelled"}, "NOTIFICACION"),
        ({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}, "RESPUESTA"),
        ({"jsonrpc": "2.0", "id": 4, "error": {"code": -32601, "message": "x"}}, "ERROR"),
    ],
)
def test_classify_covers_every_jsonrpc_shape(message, expected_category):
    category, _ = analyzer.classify(message)
    assert category == expected_category


def test_tool_name_is_shown_for_tools_call():
    _, detail = analyzer.classify(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "search_medicines"}}
    )
    assert "search_medicines" in detail


def test_domain_error_is_a_response_not_a_protocol_error():
    """isError=true is a successful JSON-RPC exchange: the tool refused, not the protocol."""
    category, detail = analyzer.classify(
        {"jsonrpc": "2.0", "id": 5, "result": {"isError": True, "content": []}}
    )
    assert category == "RESPUESTA"
    assert "isError=true" in detail


def test_decode_bodies_reads_hex_and_skips_noise():
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    response = {"jsonrpc": "2.0", "id": 1, "result": {}}
    decoded = analyzer.decode_bodies(f"{hexed(request)},{hexed(response)}")
    assert decoded == [request, response]
    assert analyzer.decode_bodies("") == []
    assert analyzer.decode_bodies(binascii.hexlify(b"not json").decode()) == []


def test_tcp_flags_are_named():
    assert analyzer.tcp_flags("0x0002") == "SYN"
    assert analyzer.tcp_flags("0x0012") == "SYN,ACK"
    assert analyzer.tcp_flags("0x0011") == "ACK,FIN"
    assert analyzer.tcp_flags("") == "-"
