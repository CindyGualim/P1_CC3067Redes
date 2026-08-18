"""Translate an MCP ``inputSchema`` into a Gemini ``FunctionDeclaration``.

This is the interoperability point the whole protocol exists for: the pharmacy
server publishes plain JSON Schema and knows nothing about Gemini, while the
host turns that same schema into whatever its provider expects. Swapping the
LLM means rewriting this file and nothing else.

Two mismatches have to be smoothed over:

* Gemini types are an uppercase enum (``OBJECT``), JSON Schema uses lowercase.
* Gemini rejects keywords it does not know, so unsupported ones are dropped
  rather than forwarded.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from google.genai import types

from host.llm.base import ToolSpec

logger = logging.getLogger(__name__)

#: JSON Schema type -> Gemini type.
_TYPES = {
    "string": types.Type.STRING,
    "number": types.Type.NUMBER,
    "integer": types.Type.INTEGER,
    "boolean": types.Type.BOOLEAN,
    "array": types.Type.ARRAY,
    "object": types.Type.OBJECT,
}


def to_gemini_schema(schema: Dict[str, Any]) -> Optional[types.Schema]:
    """Convert one JSON Schema node. Returns None when there is nothing to send."""
    if not isinstance(schema, dict):
        return None

    json_type = schema.get("type", "object")
    gemini_type = _TYPES.get(json_type)
    if gemini_type is None:
        logger.debug("Unsupported JSON Schema type '%s', sending as string", json_type)
        gemini_type = types.Type.STRING

    node = types.Schema(type=gemini_type)
    if schema.get("description"):
        node.description = schema["description"]
    if schema.get("enum"):
        # Gemini only accepts string enums; the values are already strings here.
        node.enum = [str(value) for value in schema["enum"]]
    if "minimum" in schema:
        node.minimum = float(schema["minimum"])
    if "maximum" in schema:
        node.maximum = float(schema["maximum"])

    if gemini_type == types.Type.OBJECT:
        properties = {
            key: to_gemini_schema(value)
            for key, value in (schema.get("properties") or {}).items()
        }
        properties = {key: value for key, value in properties.items() if value is not None}
        if not properties:
            # An OBJECT with no properties is rejected by the API; a tool that
            # takes no arguments must declare no parameters at all.
            return None
        node.properties = properties
        if schema.get("required"):
            node.required = list(schema["required"])

    if gemini_type == types.Type.ARRAY:
        node.items = to_gemini_schema(schema.get("items") or {"type": "string"})

    return node


def to_function_declaration(tool: ToolSpec) -> types.FunctionDeclaration:
    """Build the declaration the model sees for one MCP tool."""
    return types.FunctionDeclaration(
        name=tool.name,
        description=tool.description or tool.name,
        parameters=to_gemini_schema(tool.input_schema),
    )


def to_gemini_tools(tools: Sequence[ToolSpec]) -> List[types.Tool]:
    """Wrap every declaration in the single ``Tool`` entry the API expects."""
    declarations = [to_function_declaration(tool) for tool in tools]
    return [types.Tool(function_declarations=declarations)] if declarations else []
