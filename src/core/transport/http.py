"""Streamable HTTP transport for a remote MCP server.

The protocol remains hand-written: this module only carries decoded JSON-RPC
objects over HTTP.  A background reader consumes responses from an asyncio
queue so :class:`MCPClient` can use HTTP through the same Transport interface
as stdio.
"""

from __future__ import annotations

import asyncio
import http.client
import json
from typing import Any, AsyncIterator, Dict, Mapping, Optional
from urllib.parse import urlsplit

from core.transport.base import Transport, TransportError

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_CLOSED = object()


class HttpTransport(Transport):
    def __init__(
        self,
        url: str,
        *,
        name: str = "http",
        headers: Optional[Mapping[str, str]] = None,
        timeout: float = 60.0,
    ) -> None:
        self.url = url
        self.name = name
        self.headers = dict(headers or {})
        self.timeout = timeout
        self._parsed = urlsplit(url)
        self._path = self._parsed.path or "/mcp"
        if self._parsed.query:
            self._path += "?" + self._parsed.query
        self._responses: asyncio.Queue[Any] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._connection: http.client.HTTPConnection | None = None
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._parsed.scheme not in {"http", "https"} or not self._parsed.hostname:
            raise TransportError(f"Invalid MCP HTTP URL: {self.url}")
        self._started = True

    def set_protocol_version(self, version: str) -> None:
        self.headers["MCP-Protocol-Version"] = version

    def _new_connection(self) -> http.client.HTTPConnection:
        connection_type = (
            http.client.HTTPSConnection
            if self._parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        return connection_type(
            self._parsed.hostname,
            self._parsed.port,
            timeout=self.timeout,
        )

    async def send(self, message: Dict[str, Any]) -> None:
        if not self._started or self._closed:
            raise TransportError(f"Transport '{self.name}' is not running")

        async with self._send_lock:
            response = await asyncio.to_thread(self._send_blocking, message)
        if response is not None:
            await self._responses.put(response)

    def _send_blocking(self, message: Dict[str, Any]) -> Dict[str, Any] | None:
        payload = json.dumps(
            message, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            **self.headers,
        }

        try:
            if self._connection is None:
                self._connection = self._new_connection()
            self._connection.request("POST", self._path, body=payload, headers=headers)
            response = self._connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, http.client.HTTPException) as exc:
            self._drop_connection()
            raise TransportError(f"HTTP request to '{self.name}' failed: {exc}") from exc

        if len(body) > MAX_RESPONSE_BYTES:
            self._drop_connection()
            raise TransportError(f"Response from '{self.name}' exceeds 5 MiB")
        if response.status == 202:
            return None
        if response.status < 200 or response.status >= 300:
            detail = body.decode("utf-8", errors="replace")[:1000]
            raise TransportError(
                f"Remote MCP server '{self.name}' returned HTTP "
                f"{response.status}: {detail}"
            )
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError(
                f"Remote MCP server '{self.name}' returned invalid JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise TransportError("Remote MCP response must be a JSON object")
        return decoded

    def _drop_connection(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    async def receive(self) -> AsyncIterator[Dict[str, Any]]:
        while True:
            item = await self._responses.get()
            if item is _CLOSED:
                break
            yield item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._drop_connection)
        await self._responses.put(_CLOSED)
