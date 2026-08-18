"""A scripted LLM, so the agent loop can be tested without network or API key.

It records what it was asked, which is how the tests check that the
conversation context really travels to the provider on every turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

from host.llm.base import LLMClient, LLMError, ToolSpec
from host.messages import LLMTurn, Message, ToolCall


@dataclass
class Recorded:
    system_instruction: str
    messages: List[Message]
    tools: List[ToolSpec]


class FakeLLM(LLMClient):
    """Replays a fixed list of turns, one per call to ``generate``."""

    model = "fake-model"

    def __init__(self, turns: Sequence[LLMTurn], *, fail_with: str | None = None) -> None:
        self.turns = list(turns)
        self.fail_with = fail_with
        self.calls: List[Recorded] = []

    async def generate(
        self,
        *,
        system_instruction: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> LLMTurn:
        if self.fail_with:
            raise LLMError(self.fail_with)

        self.calls.append(
            Recorded(
                system_instruction=system_instruction,
                messages=list(messages),
                tools=list(tools),
            )
        )
        if not self.turns:
            return LLMTurn(text="(sin guion restante)")
        return self.turns.pop(0)


def say(text: str) -> LLMTurn:
    return LLMTurn(text=text)


def call(name: str, arguments: Dict[str, Any] | None = None, text: str = "") -> LLMTurn:
    return LLMTurn(text=text, tool_calls=[ToolCall(name=name, arguments=arguments or {})])


def call_many(*calls: ToolCall) -> LLMTurn:
    return LLMTurn(tool_calls=list(calls))
