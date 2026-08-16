"""MCP client session, implemented by hand on top of JSON-RPC 2.0.

One instance owns one connection to one MCP server. The host (chatbot) creates
one client per configured server and multiplexes the LLM's tool calls onto them.

Lifecycle enforced here, per the MCP specification:

    1. host  --> server : initialize                (request)
    2. host  <-- server : InitializeResult          (response)
    3. host  --> server : notifications/initialized (notification, no reply)
    4. ... tools/list, tools/call ... in any order, concurrently
    5. transport shutdown

Requests are correlated by ``id`` through a table of futures, so several tool
calls can be in flight at once instead of blocking the session one at a time.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from core.jsonrpc import (
    ErrorCode,
    JsonRpcErrorResponse,
    JsonRpcException,
    JsonRpcRequest,
    JsonRpcSuccessResponse,
    build_error,
    build_notification,
    build_request,
    build_success,
    parse_message,
)
from core.mcp.errors import (
    NotInitializedError,
    ProtocolVersionError,
    ServerConnectionError,
)
from core.mcp.protocol_log import ProtocolLogger
from core.mcp.types import (
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    CallToolResult,
    ClientCapabilities,
    Implementation,
    InitializeResult,
    ListToolsResult,
    Method,
    Tool,
)
from core.transport.base import Transport

logger = logging.getLogger(__name__)

DEFAULT_CLIENT_INFO = Implementation(
    name="pharmacy-mcp-host",
    version="0.1.0",
    title="Pharmacy MCP Chatbot",
)


class MCPClient:
    """A single MCP session: one transport, one server, many tool calls."""

    def __init__(
        self,
        transport: Transport,
        *,
        protocol_logger: Optional[ProtocolLogger] = None,
        client_info: Implementation = DEFAULT_CLIENT_INFO,
        request_timeout: float = 60.0,
    ) -> None:
        self.transport = transport
        self.name = transport.name
        self.protocol_logger = protocol_logger
        self.client_info = client_info
        self.request_timeout = request_timeout

        self.server_info: Optional[Implementation] = None
        self.initialize_result: Optional[InitializeResult] = None
        self.protocol_version: Optional[str] = None
        self.tools: List[Tool] = []

        self._pending: Dict[Any, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._initialized = False
        self._closed = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def connect(self) -> InitializeResult:
        """Start the transport and run the initialize handshake."""
        await self.transport.start()
        self._reader_task = asyncio.create_task(self._read_loop())

        raw = await self._request(
            Method.INITIALIZE,
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                # We consume tools only, so no client capability is advertised.
                "capabilities": ClientCapabilities().model_dump(exclude_none=True),
                "clientInfo": self.client_info.model_dump(exclude_none=True),
            },
        )
        result = InitializeResult(**raw)

        # Version negotiation: the server answers with the version it chose.
        if result.protocolVersion not in SUPPORTED_PROTOCOL_VERSIONS:
            await self.close()
            raise ProtocolVersionError(
                "Server '{0}' requires protocol {1}, this host speaks {2}".format(
                    self.name, result.protocolVersion, SUPPORTED_PROTOCOL_VERSIONS
                )
            )

        self.initialize_result = result
        self.server_info = result.serverInfo
        self.protocol_version = result.protocolVersion

        # Only after this notification may the server be used.
        await self._notify(Method.INITIALIZED)
        self._initialized = True

        logger.info(
            "Connected to '%s' (%s v%s, protocol %s)",
            self.name,
            result.serverInfo.name,
            result.serverInfo.version,
            result.protocolVersion,
        )
        return result

    async def close(self) -> None:
        """Tear the session down, failing anything still waiting for a reply."""
        if self._closed:
            return
        self._closed = True
        self._initialized = False

        for future in self._pending.values():
            if not future.done():
                future.set_exception(
                    JsonRpcException(ErrorCode.CONNECTION_CLOSED, "Session closed")
                )
        self._pending.clear()

        if self._reader_task:
            self._reader_task.cancel()
        await self.transport.close()

    async def __aenter__(self) -> "MCPClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    # MCP operations
    # ------------------------------------------------------------------ #
    async def list_tools(self, *, refresh: bool = False) -> List[Tool]:
        """Fetch the server catalogue, following cursor pagination."""
        if self.tools and not refresh:
            return self.tools

        collected: List[Tool] = []
        cursor: Optional[str] = None
        while True:
            params = {"cursor": cursor} if cursor else None
            page = ListToolsResult(**await self._request(Method.TOOLS_LIST, params))
            collected.extend(page.tools)
            cursor = page.nextCursor
            if not cursor:
                break

        self.tools = collected
        logger.info(
            "Server '%s' exposes %d tool(s): %s",
            self.name,
            len(collected),
            ", ".join(tool.name for tool in collected) or "-",
        )
        return collected

    async def call_tool(
        self, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> CallToolResult:
        """Invoke a tool.

        A JSON-RPC error (unknown tool, invalid params) is converted into a
        ``CallToolResult`` with ``isError=True`` so the LLM always receives
        something it can read and recover from, instead of the turn blowing up.
        """
        try:
            raw = await self._request(
                Method.TOOLS_CALL, {"name": name, "arguments": arguments or {}}
            )
        except JsonRpcException as exc:
            logger.warning("Tool '%s' failed on '%s': %s", name, self.name, exc)
            return CallToolResult(
                content=[
                    {"type": "text", "text": f"MCP error {exc.code}: {exc.message}"}
                ],
                isError=True,
            )
        return CallToolResult(**raw)

    async def ping(self) -> bool:
        """Liveness check; also used as a keep-alive over HTTP later on."""
        try:
            await self._request(Method.PING, None)
            return True
        except (JsonRpcException, asyncio.TimeoutError):
            return False

    # ------------------------------------------------------------------ #
    # JSON-RPC plumbing
    # ------------------------------------------------------------------ #
    async def _request(self, method: str, params: Optional[Dict[str, Any]]) -> Any:
        """Send a request and await the response carrying the same id."""
        if self._closed:
            raise ServerConnectionError(f"Session '{self.name}' is closed")
        if not self._initialized and method != Method.INITIALIZE:
            raise NotInitializedError(
                f"Cannot call '{method}' before initialize on '{self.name}'"
            )

        request = build_request(method, params)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request.id] = future

        await self._send(request.to_wire())
        try:
            return await asyncio.wait_for(future, timeout=self.request_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request.id, None)
            # Tell the server to stop working on it. The cancellation is a
            # notification, so there is no reply to wait for.
            await self._notify(
                Method.CANCELLED, {"requestId": request.id, "reason": "timeout"}
            )
            raise JsonRpcException(
                ErrorCode.REQUEST_TIMEOUT,
                f"'{method}' timed out after {self.request_timeout}s on '{self.name}'",
            )

    async def _notify(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> None:
        await self._send(build_notification(method, params).to_wire())

    async def _send(self, payload: Dict[str, Any]) -> None:
        if self.protocol_logger:
            self.protocol_logger.record(
                server=self.name, direction="outgoing", payload=payload
            )
        await self.transport.send(payload)

    async def _read_loop(self) -> None:
        """Dispatch every inbound message until the transport closes."""
        try:
            async for payload in self.transport.receive():
                if self.protocol_logger:
                    self.protocol_logger.record(
                        server=self.name, direction="incoming", payload=payload
                    )
                try:
                    await self._dispatch(payload)
                except Exception:
                    logger.exception("[%s] Failed to dispatch message", self.name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[%s] Reader loop crashed", self.name)
        finally:
            # The peer is gone: unblock anybody still waiting on a response.
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(
                        JsonRpcException(
                            ErrorCode.CONNECTION_CLOSED,
                            f"Server '{self.name}' closed the connection",
                        )
                    )
            self._pending.clear()

    async def _dispatch(self, payload: Dict[str, Any]) -> None:
        """Route one message to the future, the request handler or the log."""
        message = parse_message(payload)

        if isinstance(message, (JsonRpcSuccessResponse, JsonRpcErrorResponse)):
            self._resolve_pending(message)
        elif isinstance(message, JsonRpcRequest):
            # Server initiated request: it carries an id, so it must be answered.
            await self._handle_server_request(message)
        else:
            await self._handle_notification(payload)

    def _resolve_pending(self, message: Any) -> None:
        future = self._pending.pop(message.id, None)
        if future is None or future.done():
            logger.debug("[%s] Response for unknown id %s", self.name, message.id)
            return
        if isinstance(message, JsonRpcErrorResponse):
            err = message.error
            future.set_exception(JsonRpcException(err.code, err.message, err.data))
        else:
            future.set_result(message.result)

    async def _handle_server_request(self, request: JsonRpcRequest) -> None:
        if request.method == Method.PING:
            await self._send(build_success(request.id, {}).to_wire())
            return
        # roots/list, sampling/createMessage and elicitation/* are not
        # advertised by this host, so declining them is the specified behaviour.
        await self._send(
            build_error(
                request.id,
                ErrorCode.METHOD_NOT_FOUND,
                f"Host does not implement '{request.method}'",
            ).to_wire()
        )

    async def _handle_notification(self, payload: Dict[str, Any]) -> None:
        method = payload.get("method")
        if method == Method.TOOLS_LIST_CHANGED:
            logger.info("[%s] Tool list changed, refreshing catalogue", self.name)
            await self.list_tools(refresh=True)
        elif method == Method.LOG_MESSAGE:
            params = payload.get("params") or {}
            logger.info("[%s log] %s", self.name, params.get("data"))
        else:
            logger.debug("[%s] Notification '%s'", self.name, method)
