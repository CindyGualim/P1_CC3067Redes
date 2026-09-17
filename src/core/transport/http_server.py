"""Minimal Streamable HTTP server for the hand-written MCP implementation."""

from __future__ import annotations

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Type

from core.mcp.server import MCPServer, dumps
from core.mcp.types import SUPPORTED_PROTOCOL_VERSIONS

logger = logging.getLogger(__name__)
DEFAULT_MAX_BODY_BYTES = 1024 * 1024


def create_http_server(
    mcp_server: MCPServer,
    host: str,
    port: int,
    *,
    auth_token: str,
    allowed_origin: Optional[str] = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> HTTPServer:
    """Bind an HTTP server around an existing transport-independent MCP server."""

    handler = _make_handler(
        mcp_server,
        auth_token=auth_token,
        allowed_origin=allowed_origin,
        max_body_bytes=max_body_bytes,
    )
    return HTTPServer((host, port), handler)


def serve_http(
    mcp_server: MCPServer,
    host: str,
    port: int,
    *,
    auth_token: str,
    allowed_origin: Optional[str] = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> None:
    httpd = create_http_server(
        mcp_server,
        host,
        port,
        auth_token=auth_token,
        allowed_origin=allowed_origin,
        max_body_bytes=max_body_bytes,
    )
    logger.info("Remote MCP server listening on http://%s:%s/mcp", host, httpd.server_port)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def _make_handler(
    mcp_server: MCPServer,
    *,
    auth_token: str,
    allowed_origin: Optional[str],
    max_body_bytes: int,
) -> Type[BaseHTTPRequestHandler]:
    class MCPRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "PharmacyMCP/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            logger.info("HTTP %s - %s", self.address_string(), format % args)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "server": mcp_server.info.name,
                        "version": mcp_server.info.version,
                    },
                )
                return
            self._method_not_allowed("POST, OPTIONS" if self.path == "/mcp" else None)

        def do_DELETE(self) -> None:  # noqa: N802
            self._method_not_allowed("POST, OPTIONS" if self.path == "/mcp" else None)

        def do_OPTIONS(self) -> None:  # noqa: N802
            if self.path != "/mcp":
                self._method_not_allowed(None)
                return
            headers = {
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": (
                    "Authorization, Content-Type, MCP-Protocol-Version"
                ),
            }
            self._empty(HTTPStatus.NO_CONTENT, headers)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/mcp":
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return
            if auth_token and self.headers.get("Authorization") != f"Bearer {auth_token}":
                self._json(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": "Missing or invalid bearer token"},
                    {"WWW-Authenticate": "Bearer"},
                )
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
            if content_type != "application/json":
                self._json(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    {"error": "Content-Type must be application/json"},
                )
                return
            protocol_version = self.headers.get("MCP-Protocol-Version")
            if protocol_version and protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": f"Unsupported MCP protocol version: {protocol_version}"},
                )
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"})
                return
            if length <= 0 or length > max_body_bytes:
                self._json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"error": f"Request body must be between 1 and {max_body_bytes} bytes"},
                )
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body"})
                return

            response = mcp_server.handle_message(payload)
            if response is None:
                self._empty(HTTPStatus.ACCEPTED)
            else:
                self._json(HTTPStatus.OK, response)

        def _cors_headers(self) -> Dict[str, str]:
            origin = self.headers.get("Origin")
            if allowed_origin and origin == allowed_origin:
                return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
            return {}

        def _json(
            self,
            status: HTTPStatus,
            payload: Dict[str, Any],
            headers: Optional[Dict[str, str]] = None,
        ) -> None:
            body = dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in {**self._cors_headers(), **(headers or {})}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _empty(
            self, status: HTTPStatus, headers: Optional[Dict[str, str]] = None
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            for key, value in {**self._cors_headers(), **(headers or {})}.items():
                self.send_header(key, value)
            self.end_headers()

        def _method_not_allowed(self, allow: Optional[str]) -> None:
            headers = {"Allow": allow} if allow else None
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Method not allowed"}, headers)

    return MCPRequestHandler

