"""Transport abstraction.

MCP separates *what* is said (JSON-RPC messages) from *how* it travels. The
first delivery only needs stdio, but the second one replaces it with Streamable
HTTP against Cloud Run; keeping this interface means the MCP client and the
pharmacy tool handlers stay untouched when that happens.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Dict


class Transport(ABC):
    """Bidirectional channel that carries whole JSON-RPC messages."""

    #: Human readable name, used by the protocol logger and the TUI.
    name: str = "transport"

    @abstractmethod
    async def start(self) -> None:
        """Open the channel. Must be called before any send/receive."""

    @abstractmethod
    async def send(self, message: Dict[str, Any]) -> None:
        """Write one JSON-RPC message to the peer."""

    @abstractmethod
    def receive(self) -> AsyncIterator[Dict[str, Any]]:
        """Yield decoded JSON-RPC messages until the peer closes the channel."""

    @abstractmethod
    async def close(self) -> None:
        """Release the channel and any OS resources it owns."""


class TransportError(RuntimeError):
    """Raised when the channel is unusable (not started, already closed, ...)."""
