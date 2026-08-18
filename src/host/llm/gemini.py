"""Gemini client (requirement 1: talking to an LLM at the API level).

Automatic function calling is switched off on purpose. The SDK can execute
Python callables by itself, but the point of this project is that the tools live
behind MCP: the model only names a tool, and the agent decides whether to run
it, asks the user when it writes, and forwards the result back.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Sequence

from google import genai
from google.genai import types

from host.llm.base import LLMClient, LLMError, ToolSpec
from host.llm.schema import to_gemini_tools
from host.messages import LLMTurn, Message, ToolCall

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiClient(LLMClient):
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        temperature: float = 0.2,
        max_output_tokens: int = 2048,
    ) -> None:
        if not api_key:
            raise LLMError(
                "Falta GEMINI_API_KEY. Copie .env.example a .env y agregue su llave "
                "de https://aistudio.google.com/apikey"
            )
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens

    async def generate(
        self,
        *,
        system_instruction: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> LLMTurn:
        config = types.GenerateContentConfig(
            system_instruction=system_instruction or None,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            tools=to_gemini_tools(tools),
            # The agent runs the tools, not the SDK.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        try:
            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents=to_gemini_contents(messages),
                config=config,
            )
        except Exception as exc:  # network, quota, invalid key, safety block
            raise LLMError(f"Gemini rechazo la solicitud: {exc}") from exc

        return _parse_response(response)

    async def close(self) -> None:
        # The SDK owns its httpx pool and closes it with the process.
        return None


def to_gemini_contents(messages: Sequence[Message]) -> List[types.Content]:
    """Map the host history onto the ``contents`` list of the API.

    Gemini has two roles, ``user`` and ``model``; tool results travel as a user
    turn carrying function-response parts.
    """
    contents: List[types.Content] = []
    for message in messages:
        if message.role == "user":
            contents.append(
                types.Content(role="user", parts=[types.Part.from_text(text=message.text)])
            )

        elif message.role == "assistant":
            parts: List[types.Part] = []
            if message.text:
                parts.append(types.Part.from_text(text=message.text))
            for call in message.tool_calls:
                parts.append(
                    types.Part.from_function_call(name=call.name, args=call.arguments)
                )
            if parts:
                contents.append(types.Content(role="model", parts=parts))

        elif message.role == "tool":
            parts = [
                types.Part.from_function_response(
                    name=result.call.name,
                    # The API requires an object here, never a bare string.
                    response={
                        "result": result.text,
                        "isError": result.is_error,
                        **({"data": result.data} if result.data is not None else {}),
                    },
                )
                for result in message.tool_results
            ]
            if parts:
                contents.append(types.Content(role="user", parts=parts))

    return contents


def _parse_response(response: Any) -> LLMTurn:
    """Pull text and function calls out of the first candidate."""
    text_parts: List[str] = []
    tool_calls: List[ToolCall] = []

    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        raise LLMError("Gemini no devolvio ninguna respuesta (posible bloqueo de seguridad).")

    content = getattr(candidates[0], "content", None)
    for part in getattr(content, "parts", None) or []:
        if getattr(part, "text", None):
            text_parts.append(part.text)
        call = getattr(part, "function_call", None)
        if call is not None and getattr(call, "name", None):
            tool_calls.append(
                ToolCall(name=call.name, arguments=_normalize_args(call.args))
            )

    return LLMTurn(
        text="".join(text_parts).strip(),
        tool_calls=tool_calls,
        usage=_usage(response),
    )


def _normalize_args(args: Any) -> Dict[str, Any]:
    """The SDK returns a mapping, but proto structs sometimes arrive as JSON."""
    if args is None:
        return {}
    if isinstance(args, dict):
        return dict(args)
    try:
        return json.loads(json.dumps(dict(args)))
    except Exception:
        logger.warning("Could not read tool arguments: %r", args)
        return {}


def _usage(response: Any) -> Dict[str, int] | None:
    metadata = getattr(response, "usage_metadata", None)
    if metadata is None:
        return None
    return {
        "prompt_tokens": getattr(metadata, "prompt_token_count", 0) or 0,
        "response_tokens": getattr(metadata, "candidates_token_count", 0) or 0,
        "total_tokens": getattr(metadata, "total_token_count", 0) or 0,
    }
