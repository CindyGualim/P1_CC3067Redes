"""Translation from MCP to Gemini, and the conversation buffer.

These run offline: they exercise the mapping code and the SDK's own validation,
never the network.
"""

import pytest
from google.genai import types

from host.conversation import Conversation
from host.llm.base import ToolSpec
from host.llm.gemini import to_gemini_contents
from host.llm.schema import to_function_declaration, to_gemini_schema, to_gemini_tools
from host.messages import ToolCall, ToolResult

ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "branch_id": {"type": "string", "description": "Sucursal"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sku": {"type": "string"},
                    "quantity": {"type": "integer", "minimum": 1},
                },
                "required": ["sku", "quantity"],
            },
        },
    },
    "required": ["branch_id", "items"],
}


# --------------------------------------------------------------------------- #
# Schema translation
# --------------------------------------------------------------------------- #
def test_object_schema_keeps_properties_and_required():
    schema = to_gemini_schema(ORDER_SCHEMA)
    assert schema.type == types.Type.OBJECT
    assert set(schema.properties) == {"branch_id", "items"}
    assert schema.required == ["branch_id", "items"]


def test_nested_array_of_objects_is_translated():
    schema = to_gemini_schema(ORDER_SCHEMA)
    item = schema.properties["items"].items
    assert item.type == types.Type.OBJECT
    assert item.properties["quantity"].type == types.Type.INTEGER
    assert item.properties["quantity"].minimum == 1


def test_enum_is_preserved():
    schema = to_gemini_schema(
        {"type": "object", "properties": {"f": {"type": "string", "enum": ["a", "b"]}}}
    )
    assert schema.properties["f"].enum == ["a", "b"]


def test_tool_without_arguments_declares_no_parameters():
    """Gemini rejects an OBJECT with no properties, so it must be omitted."""
    schema = to_gemini_schema({"type": "object", "properties": {}, "required": []})
    assert schema is None

    declaration = to_function_declaration(
        ToolSpec(name="pharmacy__list_branches", description="lista", input_schema={
            "type": "object", "properties": {}, "required": []
        })
    )
    assert declaration.parameters is None
    assert declaration.name == "pharmacy__list_branches"


def test_unknown_json_type_falls_back_to_string():
    schema = to_gemini_schema({"type": "object", "properties": {"x": {"type": "null"}}})
    assert schema.properties["x"].type == types.Type.STRING


def test_tools_are_wrapped_in_a_single_tool_entry():
    specs = [
        ToolSpec(name="a__one", description="d", input_schema=ORDER_SCHEMA),
        ToolSpec(name="a__two", description="d", input_schema=ORDER_SCHEMA),
    ]
    tools = to_gemini_tools(specs)
    assert len(tools) == 1
    assert [d.name for d in tools[0].function_declarations] == ["a__one", "a__two"]
    assert to_gemini_tools([]) == []


# --------------------------------------------------------------------------- #
# History mapping
# --------------------------------------------------------------------------- #
def test_history_maps_to_user_and_model_roles():
    conversation = Conversation()
    conversation.add_user("hola")
    call = ToolCall(name="pharmacy__list_branches", arguments={})
    conversation.add_assistant(text="", tool_calls=[call])
    conversation.add_tool_results([ToolResult(call=call, text="tres sucursales")])
    conversation.add_assistant(text="Tenemos tres.")

    contents = to_gemini_contents(conversation.messages)
    assert [content.role for content in contents] == ["user", "model", "user", "model"]

    # The tool call travels as a function_call part, the answer as a response part.
    assert contents[1].parts[0].function_call.name == "pharmacy__list_branches"
    response = contents[2].parts[0].function_response
    assert response.name == "pharmacy__list_branches"
    assert response.response["result"] == "tres sucursales"


def test_assistant_message_without_content_is_skipped():
    conversation = Conversation()
    conversation.add_user("hola")
    conversation.add_assistant(text="")
    assert len(to_gemini_contents(conversation.messages)) == 1


# --------------------------------------------------------------------------- #
# Conversation buffer
# --------------------------------------------------------------------------- #
def test_trimming_never_orphans_a_tool_result():
    conversation = Conversation(max_messages=4)
    for index in range(3):
        conversation.add_user(f"pregunta {index}")
        call = ToolCall(name="pharmacy__list_branches", arguments={})
        conversation.add_assistant(tool_calls=[call])
        conversation.add_tool_results([ToolResult(call=call, text="ok")])

    assert len(conversation) <= 4
    # A tool result may never be the first message: the model turn it answers
    # would be missing and the API would reject the request.
    assert conversation.messages[0].role != "tool"


def test_last_answer_and_transcript():
    conversation = Conversation()
    conversation.add_user("hola")
    conversation.add_assistant("buenas")
    assert conversation.last_answer == "buenas"
    assert "Usuario: hola" in conversation.transcript()


def test_reset_keeps_the_system_instruction():
    conversation = Conversation(system_instruction="reglas")
    conversation.add_user("hola")
    conversation.reset()
    assert len(conversation) == 0
    assert conversation.system_instruction == "reglas"


def test_tool_call_splits_server_and_tool():
    call = ToolCall(name="pharmacy__create_purchase_order", arguments={})
    assert call.server == "pharmacy"
    assert call.tool == "create_purchase_order"


def test_gemini_client_requires_a_key():
    from host.llm.base import LLMError
    from host.llm.gemini import GeminiClient

    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiClient(api_key="")
