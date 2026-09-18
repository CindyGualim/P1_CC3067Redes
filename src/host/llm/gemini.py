"""Gemini client (requirement 1: talking to an LLM at the API level).

Automatic function calling is switched off on purpose. The SDK can execute
Python callables by itself, but the point of this project is that the tools live
behind MCP: the model only names a tool, and the agent decides whether to run
it, asks the user when it writes, and forwards the result back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Dict, List, Sequence

from google import genai
from google.genai import types

from host.llm.base import LLMClient, LLMError, ToolSpec
from host.llm.schema import to_gemini_tools
from host.messages import LLMTurn, Message, ToolCall

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.5-flash-lite"

MAX_RETRIES = 5
RETRY_BASE_DELAY = 1.0
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
RETRYABLE_ERROR_CODES = {"ECONNRESET", "ETIMEDOUT", "EAI_AGAIN", "ENETUNREACH"}


def is_retryable_error(error: Exception) -> bool:
    """Classify temporary provider and network failures without SDK coupling."""
    status = getattr(error, "status_code", None)
    code = getattr(error, "code", None)
    response = getattr(error, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    if status is None and isinstance(code, int):
        status = code
    if status in RETRYABLE_STATUS_CODES:
        return True
    if str(code).upper() in RETRYABLE_ERROR_CODES:
        return True

    text = str(error).lower()
    status_markers = [f" {status_code}" for status_code in RETRYABLE_STATUS_CODES]
    network_markers = (
        "timed out",
        "timeout",
        "connection reset",
        "temporarily unavailable",
        "temporary failure",
        "name resolution",
    )
    return any(marker in text for marker in (*status_markers, *network_markers))


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

        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await self.client.aio.models.generate_content(
                    model=self.model,
                    contents=to_gemini_contents(messages),
                    config=config,
                )
                return _parse_response(response)
            except Exception as exc:
                last_exc = exc
                if is_retryable_error(exc) and attempt < MAX_RETRIES:
                    delay = min(30.0, RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                    delay += random.uniform(0, 0.25)
                    logger.warning(
                        "Fallo temporal de Gemini, reintento %d/%d en %.1fs: %s",
                        attempt,
                        MAX_RETRIES,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise LLMError(f"Gemini rechazo la solicitud: {exc}") from exc
        raise LLMError(f"Gemini rechazo la solicitud tras {MAX_RETRIES} intentos: {last_exc}") from last_exc

    async def close(self) -> None:
        # The SDK owns its httpx pool and closes it with the process.
        return None


def to_gemini_contents(messages: Sequence[Message]) -> List[types.Content]:
    """Map the host history onto the ``contents`` list of the API.

    Gemini has two roles, ``user`` and ``model``; tool results travel as a user
    turn carrying function-response parts.

    ``raw_parts`` on an assistant message is the original Content object from the
    API response, preserving thought_signature.  When it is missing (old session
    or error recovery), function-call parts are omitted to avoid the
    INVALID_ARGUMENT error, and the corresponding tool results are inlined as
    text so the model still has context.
    """
    contents: List[types.Content] = []
    _skip_tool_results = False

    for message in messages:
        if message.role == "user":
            _skip_tool_results = False
            contents.append(
                types.Content(role="user", parts=[types.Part.from_text(text=message.text)])
            )

        elif message.role == "assistant":
            if message.raw_parts:
                _skip_tool_results = False
                contents.append(message.raw_parts)
            else:
                parts: List[types.Part] = []
                if message.text:
                    parts.append(types.Part.from_text(text=message.text))
                if message.tool_calls:
                    _skip_tool_results = True
                    if not parts:
                        names = ", ".join(c.name for c in message.tool_calls)
                        parts.append(types.Part.from_text(text=f"[Consulté: {names}]"))
                else:
                    _skip_tool_results = False
                if parts:
                    contents.append(types.Content(role="model", parts=parts))

        elif message.role == "tool":
            if _skip_tool_results:
                text_lines = []
                for result in message.tool_results:
                    status = "error" if result.is_error else "ok"
                    text_lines.append(
                        f"[{result.call.name} → {status}: {result.text[:500]}]"
                    )
                if text_lines:
                    contents.append(
                        types.Content(
                            role="user",
                            parts=[types.Part.from_text(text="\n".join(text_lines))],
                        )
                    )
                _skip_tool_results = False
            else:
                parts = [
                    types.Part.from_function_response(
                        name=result.call.name,
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
    raw_parts = getattr(content, "parts", None) or []
    for part in raw_parts:
        if getattr(part, "text", None) and not getattr(part, "thought", False):
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
        raw_parts=content if tool_calls else None,
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
