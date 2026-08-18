"""Conversation state (requirement 2 of the project statement).

Holds the full exchange so a follow-up question resolves against what was said
before: "¿y ese cuánto cuesta?" only means something if the previous turn is
still in the history.

The history is trimmed by turns, never mid-turn: a model message that requested
tools must always keep the tool results that answer it, otherwise the next
request to the API is invalid.
"""

from __future__ import annotations

from typing import Iterator, List, Optional

from host.messages import Message, ToolCall, ToolResult


class Conversation:
    def __init__(self, system_instruction: str = "", max_messages: int = 60) -> None:
        self.system_instruction = system_instruction
        self.max_messages = max_messages
        self._messages: List[Message] = []

    # -- writing ----------------------------------------------------------- #
    def add_user(self, text: str) -> Message:
        return self._append(Message(role="user", text=text))

    def add_assistant(self, text: str = "", tool_calls: Optional[List[ToolCall]] = None) -> Message:
        return self._append(Message(role="assistant", text=text, tool_calls=tool_calls or []))

    def add_tool_results(self, results: List[ToolResult]) -> Message:
        return self._append(Message(role="tool", tool_results=results))

    def _append(self, message: Message) -> Message:
        self._messages.append(message)
        self._trim()
        return message

    def _trim(self) -> None:
        """Drop the oldest complete turns once the history grows too long."""
        while len(self._messages) > self.max_messages:
            # Never leave a tool result orphaned at the front of the history.
            del self._messages[0]
            while self._messages and self._messages[0].role == "tool":
                del self._messages[0]

    # -- reading ----------------------------------------------------------- #
    @property
    def messages(self) -> List[Message]:
        return list(self._messages)

    def __iter__(self) -> Iterator[Message]:
        return iter(self._messages)

    def __len__(self) -> int:
        return len(self._messages)

    @property
    def last_answer(self) -> str:
        for message in reversed(self._messages):
            if message.role == "assistant" and message.text:
                return message.text
        return ""

    def reset(self) -> None:
        """Start a new session, keeping the same system instruction."""
        self._messages.clear()

    def transcript(self) -> str:
        """Plain-text dump, used by the /save command of the REPL."""
        lines: List[str] = []
        for message in self._messages:
            if message.role == "user":
                lines.append(f"Usuario: {message.text}")
            elif message.role == "assistant":
                if message.text:
                    lines.append(f"Asistente: {message.text}")
                for call in message.tool_calls:
                    lines.append(f"  [herramienta] {call.name} {call.arguments}")
            else:
                for result in message.tool_results:
                    status = "ERROR" if result.is_error else "OK"
                    lines.append(f"  [resultado {status}] {result.call.name}: {result.text[:200]}")
        return "\n".join(lines)
