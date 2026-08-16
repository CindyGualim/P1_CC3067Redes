"""Protocol log (requirement #3 of the project statement).

Every JSON-RPC message that crosses the boundary between the host and any MCP
server is recorded here, in both directions, with its raw payload. Two sinks:

* a JSON Lines file under ``logs/`` so the traffic can be audited after the run
  and diffed against the Wireshark capture of the second delivery;
* a bounded in-memory ring buffer that the TUI renders live.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Literal, Optional

Direction = Literal["outgoing", "incoming"]
Kind = Literal["request", "response", "error", "notification"]


@dataclass
class ProtocolEntry:
    """One JSON-RPC message observed on a session."""

    timestamp: str
    server: str
    direction: Direction
    kind: Kind
    method: Optional[str]
    message_id: Optional[Any]
    payload: Dict[str, Any]
    elapsed_ms: Optional[float] = None
    transport: str = "stdio"
    tags: List[str] = field(default_factory=list)

    def summary(self) -> str:
        """Single line rendering used by the TUI and the console log."""
        arrow = "-->" if self.direction == "outgoing" else "<--"
        label = self.method or f"id={self.message_id}"
        took = f" ({self.elapsed_ms:.0f} ms)" if self.elapsed_ms is not None else ""
        return f"{arrow} [{self.server}] {self.kind}: {label}{took}"


class ProtocolLogger:
    """Thread-safe recorder shared by every MCP session in the host."""

    def __init__(
        self,
        log_dir: Path,
        *,
        max_entries: int = 1000,
        filename: Optional[str] = None,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        self.path = self.log_dir / (filename or f"mcp_protocol_{stamp}.jsonl")
        self.entries: Deque[ProtocolEntry] = deque(maxlen=max_entries)
        self._lock = threading.Lock()
        self._subscribers: List[Callable[[ProtocolEntry], None]] = []
        # Pending outgoing requests, used to compute the round-trip time when
        # the matching response arrives: (server, id) -> monotonic timestamp.
        self._pending: Dict[tuple, float] = {}

    def subscribe(self, callback: Callable[[ProtocolEntry], None]) -> None:
        """Register a live listener (the TUI uses this to append rows)."""
        self._subscribers.append(callback)

    def record(
        self,
        *,
        server: str,
        direction: Direction,
        payload: Dict[str, Any],
        transport: str = "stdio",
        tags: Optional[List[str]] = None,
    ) -> ProtocolEntry:
        """Classify, time and persist one message."""
        kind = self._classify(payload)
        message_id = payload.get("id")
        method = payload.get("method")
        elapsed_ms: Optional[float] = None

        key = (server, str(message_id))
        if direction == "outgoing" and kind == "request":
            self._pending[key] = time.monotonic()
        elif direction == "incoming" and kind in ("response", "error"):
            started = self._pending.pop(key, None)
            if started is not None:
                elapsed_ms = (time.monotonic() - started) * 1000

        entry = ProtocolEntry(
            timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            server=server,
            direction=direction,
            kind=kind,
            method=method,
            message_id=message_id,
            payload=payload,
            elapsed_ms=elapsed_ms,
            transport=transport,
            tags=tags or [],
        )

        with self._lock:
            self.entries.append(entry)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

        for callback in self._subscribers:
            try:
                callback(entry)
            except Exception:  # a broken UI listener must not kill the session
                pass
        return entry

    @staticmethod
    def _classify(payload: Dict[str, Any]) -> Kind:
        """Map a payload to one of the four JSON-RPC message categories.

        This is the same classification requirement #7 asks for on the Wireshark
        capture, so the report can be built straight from these logs.
        """
        if "method" in payload:
            return "request" if "id" in payload else "notification"
        if "error" in payload:
            return "error"
        return "response"

    def snapshot(self) -> List[ProtocolEntry]:
        with self._lock:
            return list(self.entries)

    def stats(self) -> Dict[str, int]:
        """Counts per category, handy for the report and the TUI footer."""
        counters: Dict[str, int] = {
            "requests": 0,
            "responses": 0,
            "errors": 0,
            "notifications": 0,
        }
        for entry in self.snapshot():
            counters[entry.kind + "s" if entry.kind != "error" else "errors"] += 1
        return counters
