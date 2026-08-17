"""Server side of MCP, written by hand on top of JSON-RPC 2.0.

This is the mirror image of ``core.mcp.client`` and it is deliberately
transport-agnostic and synchronous: ``handle_message`` takes one decoded
JSON-RPC payload and returns the payload to send back, or ``None`` when the
message was a notification (which must never be answered).

That shape is what lets the same pharmacy tools run twice:

    delivery 1:  stdin/stdout loop  -> handle_message -> stdout
    delivery 2:  HTTP POST body     -> handle_message -> HTTP response body

Only the lifecycle and the ``tools`` capability are implemented, because that
is all the host consumes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.jsonrpc import (
    ErrorCode,
    JsonRpcException,
    JsonRpcNotification,
    JsonRpcRequest,
    build_error,
    build_success,
    parse_message,
)
from core.mcp.types import (
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    Implementation,
    Method,
)

logger = logging.getLogger(__name__)


class ToolError(Exception):
    """A tool ran but could not fulfil the request.

    This is *not* a protocol error: the caller gets a successful JSON-RPC
    response whose result carries ``isError: true``, so the LLM reads the reason
    ("prescription expired", "not enough stock") and can react instead of the
    conversation breaking.
    """


@dataclass
class ToolOutput:
    """What a handler returns: a readable summary plus the structured payload."""

    text: str
    data: Optional[Dict[str, Any]] = None

    def to_result(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "content": [{"type": "text", "text": self.text}],
            "isError": False,
        }
        if self.data is not None:
            result["structuredContent"] = self.data
        return result


@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], ToolOutput]
    title: Optional[str] = None
    annotations: Dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> Dict[str, Any]:
        """Serialize as the ``Tool`` object of the MCP specification."""
        tool: Dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.title:
            tool["title"] = self.title
        if self.annotations:
            tool["annotations"] = self.annotations
        return tool


# --------------------------------------------------------------------------- #
# Argument validation
# --------------------------------------------------------------------------- #
_JSON_TYPES: Dict[str, Any] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate_arguments(schema: Dict[str, Any], arguments: Dict[str, Any]) -> None:
    """Check ``arguments`` against the subset of JSON Schema the tools use.

    A full JSON Schema implementation is out of scope; required keys, types,
    enums and numeric bounds are what actually protects the handlers from a
    hallucinated argument, and everything the LLM sends goes through here.

    Raises:
        JsonRpcException: INVALID_PARAMS, which the client surfaces to the LLM.
    """
    if not isinstance(arguments, dict):
        raise JsonRpcException(ErrorCode.INVALID_PARAMS, "'arguments' must be an object")

    for key in schema.get("required", []):
        if key not in arguments or arguments[key] is None:
            raise JsonRpcException(
                ErrorCode.INVALID_PARAMS, f"Missing required argument: '{key}'"
            )

    properties = schema.get("properties", {})
    for key, value in arguments.items():
        spec = properties.get(key)
        if spec is None:
            # Unknown keys are dropped rather than rejected: models like to add
            # extras, and failing the call over one would be needlessly brittle.
            continue
        _validate_value(key, value, spec)


def _validate_value(key: str, value: Any, spec: Dict[str, Any]) -> None:
    expected = spec.get("type")
    if expected and expected in _JSON_TYPES:
        # bool is a subclass of int in Python; keep the two apart.
        if expected in ("integer", "number") and isinstance(value, bool):
            raise JsonRpcException(
                ErrorCode.INVALID_PARAMS, f"Argument '{key}' must be a {expected}"
            )
        if not isinstance(value, _JSON_TYPES[expected]):
            raise JsonRpcException(
                ErrorCode.INVALID_PARAMS,
                f"Argument '{key}' must be a {expected}, got {type(value).__name__}",
            )

    if "enum" in spec and value not in spec["enum"]:
        raise JsonRpcException(
            ErrorCode.INVALID_PARAMS,
            f"Argument '{key}' must be one of {spec['enum']}, got '{value}'",
        )

    if "minimum" in spec and isinstance(value, (int, float)) and value < spec["minimum"]:
        raise JsonRpcException(
            ErrorCode.INVALID_PARAMS, f"Argument '{key}' must be >= {spec['minimum']}"
        )

    if expected == "array" and "items" in spec:
        for index, item in enumerate(value):
            _validate_value(f"{key}[{index}]", item, spec["items"])

    if expected == "object" and "properties" in spec:
        validate_arguments(spec, value)


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #
class MCPServer:
    """Routes JSON-RPC messages to registered tools."""

    def __init__(
        self,
        name: str,
        version: str,
        *,
        title: Optional[str] = None,
        instructions: Optional[str] = None,
    ) -> None:
        self.info = Implementation(name=name, version=version, title=title)
        self.instructions = instructions
        self._tools: Dict[str, ToolDefinition] = {}
        self._initialized = False
        self.negotiated_version: Optional[str] = None

    # -- registration ------------------------------------------------------ #
    def add_tool(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        handler: Callable[[Dict[str, Any]], ToolOutput],
        *,
        title: Optional[str] = None,
        annotations: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            title=title,
            annotations=annotations or {},
        )

    @property
    def tool_names(self) -> List[str]:
        return list(self._tools)

    # -- dispatch ---------------------------------------------------------- #
    def handle_message(self, payload: Any) -> Optional[Dict[str, Any]]:
        """Handle one inbound payload, returning the reply payload or None."""
        try:
            message = parse_message(payload)
        except JsonRpcException as exc:
            # The id may be unusable, so answer with null as the spec requires.
            request_id = payload.get("id") if isinstance(payload, dict) else None
            return build_error(request_id, exc.code, exc.message, exc.data).to_wire()

        if isinstance(message, JsonRpcNotification):
            self._handle_notification(message)
            return None

        if not isinstance(message, JsonRpcRequest):
            # A response arriving at a server means the peer is confused; the
            # specification says to ignore it rather than reply.
            logger.warning("Ignoring unexpected response: %s", payload)
            return None

        try:
            result = self._handle_request(message)
            return build_success(message.id, result).to_wire()
        except JsonRpcException as exc:
            return build_error(message.id, exc.code, exc.message, exc.data).to_wire()
        except Exception as exc:  # never let a handler bug kill the process
            logger.exception("Unhandled error in '%s'", message.method)
            return build_error(
                message.id, ErrorCode.INTERNAL_ERROR, f"Internal error: {exc}"
            ).to_wire()

    def _handle_notification(self, message: JsonRpcNotification) -> None:
        if message.method == Method.INITIALIZED:
            self._initialized = True
            logger.info("Client finished the handshake; server is ready")
        else:
            logger.debug("Ignoring notification '%s'", message.method)

    def _handle_request(self, request: JsonRpcRequest) -> Any:
        method = request.method
        params = request.params or {}

        if method == Method.INITIALIZE:
            return self._initialize(params)
        if method == Method.PING:
            # ping is answered at any point of the lifecycle, even before
            # initialize, so a host can probe a server it has not opened yet.
            return {}

        if not self._initialized and self.negotiated_version is None:
            raise JsonRpcException(
                ErrorCode.NOT_INITIALIZED,
                f"'{method}' was called before 'initialize'",
            )

        if method == Method.TOOLS_LIST:
            return {"tools": [tool.to_wire() for tool in self._tools.values()]}
        if method == Method.TOOLS_CALL:
            return self._call_tool(params)

        raise JsonRpcException(
            ErrorCode.METHOD_NOT_FOUND, f"Method not found: '{method}'"
        )

    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        requested = params.get("protocolVersion")
        # Honour the client's version when we speak it, otherwise answer with
        # ours and let the client decide whether it can continue.
        self.negotiated_version = (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        )
        client = params.get("clientInfo", {})
        logger.info(
            "initialize from %s v%s (protocol %s -> %s)",
            client.get("name", "unknown"),
            client.get("version", "?"),
            requested,
            self.negotiated_version,
        )

        result: Dict[str, Any] = {
            "protocolVersion": self.negotiated_version,
            # listChanged is false: the catalogue is fixed at startup.
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": self.info.model_dump(exclude_none=True),
        }
        if self.instructions:
            result["instructions"] = self.instructions
        return result

    def _call_tool(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        if not name:
            raise JsonRpcException(ErrorCode.INVALID_PARAMS, "Missing 'name'")

        tool = self._tools.get(name)
        if tool is None:
            raise JsonRpcException(
                ErrorCode.INVALID_PARAMS,
                f"Unknown tool: '{name}'. Available: {', '.join(self._tools)}",
            )

        arguments = params.get("arguments") or {}
        validate_arguments(tool.input_schema, arguments)

        try:
            return tool.handler(arguments).to_result()
        except ToolError as exc:
            # Business failure: successful response, isError flag set.
            logger.info("Tool '%s' refused: %s", name, exc)
            return {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            }


def dumps(payload: Dict[str, Any]) -> str:
    """One-line JSON encoding, the framing used by the stdio transport."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
