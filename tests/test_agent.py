"""Agent tests: the tool-calling loop, the approval gate and the context.

The registry is real — the pharmacy server runs as a subprocess — while the LLM
is scripted, so the assertions are about the host's behaviour and not about what
a model happens to answer today.
"""

import sys
from pathlib import Path

import pytest

from core.mcp.protocol_log import ProtocolLogger
from host.agent import AgentEvent, ChatAgent, build_system_prompt
from host.conversation import Conversation
from host.messages import ToolCall
from host.registry import ServerConfig, ServerRegistry, load_server_configs

sys.path.insert(0, str(Path(__file__).parent))
from fixtures.fake_llm import FakeLLM, call, call_many, say  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER = PROJECT_ROOT / "src" / "servers" / "pharmacy" / "__main__.py"


@pytest.fixture
async def registry(tmp_path):
    config = ServerConfig(
        name="pharmacy",
        command=[sys.executable, str(SERVER)],
        env={
            "PHARMACY_DB": str(tmp_path / "agent.db"),
            "PHARMACY_SEED": str(PROJECT_ROOT / "data" / "pharmacy_seed.json"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        },
    )
    reg = ServerRegistry([config], protocol_logger=ProtocolLogger(tmp_path / "logs"))
    await reg.connect_all()
    yield reg
    await reg.close_all()


def make_agent(llm, registry, *, approval=None, max_steps=6):
    events = []
    agent = ChatAgent(
        llm,
        registry,
        Conversation(system_instruction=build_system_prompt(registry)),
        approval=approval,
        on_event=events.append,
        max_steps=max_steps,
    )
    return agent, events


async def approve(_call: ToolCall, _tool) -> bool:
    return True


async def deny(_call: ToolCall, _tool) -> bool:
    return False


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #
async def test_tools_are_namespaced_by_server(registry):
    names = [tool.qualified_name for tool in registry.tools]
    assert "pharmacy__search_medicines" in names
    assert all(name.startswith("pharmacy__") for name in names)


async def test_only_the_writing_tool_needs_approval(registry):
    needs = [tool.qualified_name for tool in registry.tools if tool.requires_approval]
    assert needs == ["pharmacy__create_purchase_order"]


async def test_system_prompt_includes_server_instructions(registry):
    prompt = build_system_prompt(registry)
    assert "Farmacia Vida" in prompt
    assert "[pharmacy]" in prompt  # taken from the initialize handshake


async def test_config_file_expands_placeholders():
    configs = load_server_configs(PROJECT_ROOT / "config" / "servers.json", PROJECT_ROOT)
    pharmacy = next(config for config in configs if config.name == "pharmacy")
    assert pharmacy.command[0] == sys.executable
    assert Path(pharmacy.command[1]).exists()


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #
async def test_plain_answer_needs_no_tools(registry):
    llm = FakeLLM([say("Alan Turing fue un matematico britanico.")])
    agent, events = make_agent(llm, registry)

    answer = await agent.send("Quien fue Alan Turing?")

    assert "Turing" in answer
    assert len(llm.calls) == 1
    assert [event.kind for event in events] == ["thinking", "answer"]


async def test_read_only_tool_runs_without_asking(registry):
    llm = FakeLLM(
        [
            call("pharmacy__search_medicines", {"symptom": "dolor de cabeza"}),
            say("Le recomiendo Acetaminofen [MED-001]."),
        ]
    )
    # No approval callback at all: a read-only tool must not need one.
    agent, events = make_agent(llm, registry)

    answer = await agent.send("Me duele la cabeza")

    assert "MED-001" in answer
    kinds = [event.kind for event in events]
    assert "tool_denied" not in kinds
    assert kinds.count("tool_result") == 1


async def test_tool_output_reaches_the_next_llm_call(registry):
    llm = FakeLLM([call("pharmacy__list_branches"), say("Tenemos tres sucursales.")])
    agent, _ = make_agent(llm, registry)

    await agent.send("Donde estan ubicados?")

    # Second call to the model must carry the tool result in the history.
    second = llm.calls[1]
    tool_messages = [message for message in second.messages if message.role == "tool"]
    assert tool_messages
    assert "Farmacia Vida Zona 10" in tool_messages[0].tool_results[0].text


async def test_write_tool_is_executed_after_approval(registry):
    llm = FakeLLM(
        [
            call(
                "pharmacy__create_purchase_order",
                {
                    "branch_id": "SUC-01",
                    "customer_name": "Ana Lucia Morales",
                    "items": [{"sku": "MED-001", "quantity": 1}],
                },
            ),
            say("Su orden quedo confirmada."),
        ]
    )
    agent, events = make_agent(llm, registry, approval=approve)

    answer = await agent.send("Compreme un acetaminofen")

    assert "confirmada" in answer
    result = next(event.result for event in events if event.kind == "tool_result")
    assert result.is_error is False
    assert result.data["total"] == pytest.approx(20.72)


async def test_denied_write_tool_is_never_executed(registry):
    llm = FakeLLM(
        [
            call(
                "pharmacy__create_purchase_order",
                {
                    "branch_id": "SUC-01",
                    "customer_name": "Ana Lucia Morales",
                    "items": [{"sku": "MED-001", "quantity": 99}],
                },
            ),
            say("De acuerdo, no genere la orden."),
        ]
    )
    agent, events = make_agent(llm, registry, approval=deny)

    answer = await agent.send("Compra 99 cajas")

    assert "no genere" in answer
    assert any(event.kind == "tool_denied" for event in events)
    # The model is told why, so it can recover instead of retrying blindly.
    denied = next(event.result for event in events if event.kind == "tool_denied")
    assert denied.is_error is True
    assert "no autorizo" in denied.text

    stock = await registry.call("pharmacy__check_inventory", {"sku": "MED-001", "branch_id": "SUC-01"})
    assert stock.structuredContent["total_stock"] == 120


async def test_several_tools_in_one_turn(registry):
    llm = FakeLLM(
        [
            call_many(
                ToolCall(name="pharmacy__search_medicines", arguments={"symptom": "alergia"}),
                ToolCall(name="pharmacy__list_branches"),
            ),
            say("Tengo antihistaminicos en las tres sucursales."),
        ]
    )
    agent, events = make_agent(llm, registry)

    await agent.send("Que tienen para la alergia y donde?")

    assert [event.kind for event in events].count("tool_result") == 2


async def test_chained_tool_calls(registry):
    llm = FakeLLM(
        [
            call("pharmacy__search_medicines", {"symptom": "tos"}),
            call("pharmacy__get_medicine_details", {"sku": "MED-010"}),
            say("El ambroxol [MED-010] cuesta Q38.00."),
        ]
    )
    agent, _ = make_agent(llm, registry)

    answer = await agent.send("Algo para la tos, y dame el detalle")

    assert "Q38.00" in answer
    assert len(llm.calls) == 3


async def test_tool_error_is_handed_to_the_model(registry):
    llm = FakeLLM(
        [
            call("pharmacy__verify_prescription", {"folio": "RX-9999-9999"}),
            say("No encontre esa receta, revise el folio."),
        ]
    )
    agent, events = make_agent(llm, registry)

    await agent.send("Verifica la receta RX-9999-9999")

    result = next(event.result for event in events if event.kind == "tool_result")
    assert result.is_error is True
    assert "No se encontro" in result.text


async def test_loop_stops_at_max_steps(registry):
    llm = FakeLLM([call("pharmacy__list_branches") for _ in range(10)])
    agent, _ = make_agent(llm, registry, max_steps=3)

    answer = await agent.send("Repite para siempre")

    assert "demasiados pasos" in answer
    assert len(llm.calls) == 3


async def test_provider_failure_is_reported_not_raised(registry):
    llm = FakeLLM([], fail_with="cuota agotada")
    agent, events = make_agent(llm, registry)

    answer = await agent.send("Hola")

    assert "cuota agotada" in answer
    assert any(event.kind == "error" for event in events)


# --------------------------------------------------------------------------- #
# Context (requirement 2)
# --------------------------------------------------------------------------- #
async def test_context_is_carried_across_turns(registry):
    llm = FakeLLM([say("Alan Turing fue un matematico."), say("Nacio el 23 de junio de 1912.")])
    agent, _ = make_agent(llm, registry)

    await agent.send("Quien fue Alan Turing?")
    await agent.send("En que fecha nacio?")

    history = llm.calls[1].messages
    texts = [message.text for message in history]
    assert "Quien fue Alan Turing?" in texts
    assert "Alan Turing fue un matematico." in texts
    assert texts[-1] == "En que fecha nacio?"


async def test_reset_clears_the_context(registry):
    llm = FakeLLM([say("uno"), say("dos")])
    agent, _ = make_agent(llm, registry)

    await agent.send("primera")
    agent.conversation.reset()
    await agent.send("segunda")

    assert len(llm.calls[1].messages) == 1
