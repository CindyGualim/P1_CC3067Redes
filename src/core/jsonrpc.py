"""Manual implementation of the JSON-RPC 2.0 wire format.

The project requirements forbid using any MCP SDK, so every byte that travels
between host and server is built and validated here. This module knows nothing
about MCP: it only understands the four JSON-RPC 2.0 message shapes described in
https://www.jsonrpc.org/specification

    Request       -> has "method" and "id", expects a response.
    Notification  -> has "method" and NO "id", must never be answered.
    Success       -> has "id" and "result".
    Error         -> has "id" (possibly null) and "error".
"""

from __future__ import annotations

import itertools
from typing import Any, Dict, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

JSONRPC_VERSION = "2.0"

# Identifiers are unique per client session, not global, so a simple counter is
# enough. Kept as a module level counter so every transport shares the sequence.
_id_counter = itertools.count(1)

RequestId = Union[int, str]


def next_request_id() -> int:
    """Return a monotonically increasing id for a new outgoing request."""
    return next(_id_counter)


# --------------------------------------------------------------------------- #
# Error codes
# --------------------------------------------------------------------------- #
class ErrorCode:
    """Reserved JSON-RPC 2.0 codes plus the ones MCP adds on top."""

    # -32768 .. -32000 is the range reserved by the specification.
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    # Application-defined codes used by this project (outside the reserved range).
    REQUEST_TIMEOUT = -32000
    CONNECTION_CLOSED = -32001
    NOT_INITIALIZED = -32002


# --------------------------------------------------------------------------- #
# Message models
# --------------------------------------------------------------------------- #
class JsonRpcMessage(BaseModel):
    """Common envelope: every message carries the protocol version."""

    model_config = ConfigDict(extra="allow")

    jsonrpc: str = Field(default=JSONRPC_VERSION, pattern=r"^2\.0$")

    def to_wire(self) -> Dict[str, Any]:
        """Serialize dropping ``None`` fields.

        This matters: an omitted ``params`` and a ``"params": null`` are not the
        same thing for a strict peer, and some official MCP servers reject the
        latter.
        """
        return self.model_dump(exclude_none=True)


class JsonRpcRequest(JsonRpcMessage):
    id: RequestId
    method: str
    params: Optional[Dict[str, Any]] = None


class JsonRpcNotification(JsonRpcMessage):
    method: str
    params: Optional[Dict[str, Any]] = None


class JsonRpcErrorObj(BaseModel):
    code: int
    message: str
    data: Optional[Any] = None


class JsonRpcErrorResponse(JsonRpcMessage):
    # ``id`` is null when the request could not even be parsed.
    id: Optional[RequestId] = None
    error: JsonRpcErrorObj


class JsonRpcSuccessResponse(JsonRpcMessage):
    id: RequestId
    result: Any


JsonRpcResponse = Union[JsonRpcSuccessResponse, JsonRpcErrorResponse]
AnyJsonRpcMessage = Union[
    JsonRpcRequest, JsonRpcNotification, JsonRpcSuccessResponse, JsonRpcErrorResponse
]


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class JsonRpcException(Exception):
    """A JSON-RPC error object raised as a Python exception."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data

    def to_error_obj(self) -> JsonRpcErrorObj:
        return JsonRpcErrorObj(code=self.code, message=self.message, data=self.data)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def build_request(
    method: str, params: Optional[Dict[str, Any]] = None, request_id: Optional[RequestId] = None
) -> JsonRpcRequest:
    return JsonRpcRequest(
        id=next_request_id() if request_id is None else request_id,
        method=method,
        params=params,
    )


def build_notification(
    method: str, params: Optional[Dict[str, Any]] = None
) -> JsonRpcNotification:
    return JsonRpcNotification(method=method, params=params)


def build_success(request_id: RequestId, result: Any) -> JsonRpcSuccessResponse:
    return JsonRpcSuccessResponse(id=request_id, result=result)


def build_error(
    request_id: Optional[RequestId], code: int, message: str, data: Any = None
) -> JsonRpcErrorResponse:
    return JsonRpcErrorResponse(
        id=request_id, error=JsonRpcErrorObj(code=code, message=message, data=data)
    )


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_message(data: Any) -> AnyJsonRpcMessage:
    """Turn a decoded JSON object into the concrete message model.

    Raises:
        JsonRpcException: with INVALID_REQUEST when the payload does not match
            any of the four shapes defined by the specification.
    """
    if not isinstance(data, dict):
        raise JsonRpcException(
            ErrorCode.INVALID_REQUEST, "JSON-RPC payload must be an object", data
        )

    if data.get("jsonrpc") != JSONRPC_VERSION:
        raise JsonRpcException(
            ErrorCode.INVALID_REQUEST,
            f'Missing or invalid "jsonrpc" field, expected "{JSONRPC_VERSION}"',
            data,
        )

    has_method = "method" in data
    has_id = "id" in data

    try:
        if has_method:
            # A request is answerable, a notification is not; the only
            # difference on the wire is the presence of "id".
            return JsonRpcRequest(**data) if has_id else JsonRpcNotification(**data)
        if "result" in data:
            return JsonRpcSuccessResponse(**data)
        if "error" in data:
            return JsonRpcErrorResponse(**data)
    except JsonRpcException:
        raise
    except Exception as exc:  # pydantic ValidationError and friends
        raise JsonRpcException(
            ErrorCode.INVALID_REQUEST, f"Malformed JSON-RPC message: {exc}", data
        ) from exc

    raise JsonRpcException(
        ErrorCode.INVALID_REQUEST,
        'Payload has neither "method", "result" nor "error"',
        data,
    )


def is_response(message: AnyJsonRpcMessage) -> bool:
    """True when the message answers a previously sent request."""
    return isinstance(message, (JsonRpcSuccessResponse, JsonRpcErrorResponse))
