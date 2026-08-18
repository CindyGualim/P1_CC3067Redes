"""Provider-neutral conversation types.

The host keeps its history in these structures, not in Gemini's. Two reasons:
the LLM client stays swappable (the tests drive the agent with a fake one), and
the TUI can render a turn without knowing anything about the vendor SDK.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

Role = Literal["user", "assistant", "tool"]

_call_ids = itertools.count(1)


def next_call_id() -> str:
    return f"call-{next(_call_ids)}"


@dataclass
class ToolCall:
    """A tool invocation decided by the model."""

    name: str  # qualified as "<server>__<tool>"
    arguments: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=next_call_id)

    @property
    def server(self) -> str:
        return self.name.split("__", 1)[0]

    @property
    def tool(self) -> str:
        return self.name.split("__", 1)[1] if "__" in self.name else self.name


@dataclass
class ToolResult:
    """What the MCP server answered, ready to be fed back to the model."""

    call: ToolCall
    text: str
    is_error: bool = False
    data: Optional[Dict[str, Any]] = None


@dataclass
class Message:
    role: Role
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_results: List[ToolResult] = field(default_factory=list)


@dataclass
class LLMTurn:
    """One model response: free text, tool calls, or both."""

    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Optional[Dict[str, int]] = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)
