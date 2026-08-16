"""Unit tests for the hand-written JSON-RPC 2.0 layer."""

import pytest

from core.jsonrpc import (
    ErrorCode,
    JsonRpcErrorResponse,
    JsonRpcException,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcSuccessResponse,
    build_error,
    build_notification,
    build_request,
    build_success,
    parse_message,
)


def test_request_has_id_notification_does_not():
    request = build_request("tools/list", {"cursor": "abc"})
    notification = build_notification("notifications/initialized")

    assert request.to_wire()["id"] == request.id
    assert "id" not in notification.to_wire()


def test_to_wire_omits_empty_params():
    """A null params is not the same as an absent one for a strict peer."""
    wire = build_request("ping").to_wire()
    assert "params" not in wire
    assert wire["jsonrpc"] == "2.0"


def test_ids_are_unique_and_increasing():
    first = build_request("ping").id
    second = build_request("ping").id
    assert second > first


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"jsonrpc": "2.0", "id": 1, "method": "ping"}, JsonRpcRequest),
        ({"jsonrpc": "2.0", "method": "notifications/initialized"}, JsonRpcNotification),
        ({"jsonrpc": "2.0", "id": 1, "result": {}}, JsonRpcSuccessResponse),
        (
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "nope"}},
            JsonRpcErrorResponse,
        ),
    ],
)
def test_parse_message_detects_every_shape(payload, expected):
    assert isinstance(parse_message(payload), expected)


def test_parse_message_rejects_wrong_version():
    with pytest.raises(JsonRpcException) as exc:
        parse_message({"jsonrpc": "1.0", "id": 1, "method": "ping"})
    assert exc.value.code == ErrorCode.INVALID_REQUEST


def test_parse_message_rejects_shapeless_payload():
    with pytest.raises(JsonRpcException):
        parse_message({"jsonrpc": "2.0", "id": 1})


def test_parse_message_rejects_non_object():
    with pytest.raises(JsonRpcException):
        parse_message(["jsonrpc", "2.0"])


def test_error_response_accepts_null_id():
    """Parse errors are answered with a null id, per the specification."""
    wire = build_error(None, ErrorCode.PARSE_ERROR, "Parse error").to_wire()
    assert wire["error"]["code"] == ErrorCode.PARSE_ERROR


def test_success_roundtrip():
    wire = build_success(7, {"tools": []}).to_wire()
    parsed = parse_message(wire)
    assert isinstance(parsed, JsonRpcSuccessResponse)
    assert parsed.id == 7
