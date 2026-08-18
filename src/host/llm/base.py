"""LLM abstraction.

The agent only knows this interface, so the tool-calling loop can be tested
without network access (see ``tests/fixtures/fake_llm.py``) and a different
provider would only need a new subclass.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

from host.messages import LLMTurn, Message


@dataclass
class ToolSpec:
    """A tool as offered to the model: qualified name plus its JSON Schema."""

    name: str
    description: str
    input_schema: Dict[str, Any]


class LLMClient(ABC):
    """Single-shot completion with tool support."""

    #: Reported by the implementation, shown in the UI header.
    model: str = "unknown"

    @abstractmethod
    async def generate(
        self,
        *,
        system_instruction: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> LLMTurn:
        """Return the model's next turn given the history and the tool catalogue."""

    async def close(self) -> None:  # pragma: no cover - not every client needs it
        """Release any network resources."""


class LLMError(RuntimeError):
    """The provider could not be reached or rejected the request."""


def tool_names(tools: Sequence[ToolSpec]) -> List[str]:
    return [tool.name for tool in tools]
