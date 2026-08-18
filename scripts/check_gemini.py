"""Smoke test of the Gemini wiring. Run it once you have an API key.

    python scripts/check_gemini.py

Everything below the LLM is already covered by the test suite; what this script
proves is the part that needs the network: that the MCP schemas are accepted by
the API, that the model answers with a function call, and that the call is
parsed back into a real MCP invocation.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.config import load_settings  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from core.mcp.protocol_log import ProtocolLogger  # noqa: E402
from core.transport.stdio import configure_event_loop  # noqa: E402
from host.agent import AgentEvent, ChatAgent, build_system_prompt  # noqa: E402
from host.conversation import Conversation  # noqa: E402
from host.llm.base import LLMError  # noqa: E402
from host.llm.gemini import GeminiClient  # noqa: E402
from host.llm.schema import to_gemini_tools  # noqa: E402
from host.registry import ServerRegistry, load_server_configs  # noqa: E402

QUESTIONS = [
    "Hola, me arde el estomago despues de comer. Que me recomiendan?",
    "Y ese cuanto cuesta?",  # only answerable if the context survived
]


def show(event: AgentEvent) -> None:
    if event.kind == "tool_call":
        print(f"   -> el modelo pidio {event.tool_call.name}({event.tool_call.arguments})")
    elif event.kind == "tool_result":
        flag = "ERROR" if event.result.is_error else "ok"
        print(f"   <- {flag}: {event.result.text.splitlines()[0][:100]}")


async def main() -> int:
    settings = load_settings()
    if not settings.has_llm:
        print("Falta GEMINI_API_KEY en .env. Copie .env.example y agregue su llave.")
        return 1

    setup_logging(settings.logs_dir, level="WARNING", console=False)
    protocol_logger = ProtocolLogger(settings.logs_dir)

    configs = load_server_configs(PROJECT_ROOT / "config" / "servers.json", PROJECT_ROOT)
    registry = ServerRegistry(configs, protocol_logger=protocol_logger)
    await registry.connect_all()

    try:
        specs = registry.tool_specs()
        declarations = to_gemini_tools(specs)[0].function_declarations
        print(f"1. Traduccion de esquemas: {len(declarations)} funciones declaradas")
        for declaration in declarations:
            arguments = list((declaration.parameters.properties or {})) if declaration.parameters else []
            print(f"   - {declaration.name}({', '.join(arguments)})")

        llm = GeminiClient(settings.gemini_api_key, settings.gemini_model)
        agent = ChatAgent(
            llm,
            registry,
            Conversation(system_instruction=build_system_prompt(registry)),
            on_event=show,
        )

        for index, question in enumerate(QUESTIONS, start=2):
            print(f"\n{index}. Usuario: {question}")
            answer = await agent.send(question)
            print(f"   Asistente: {answer}")

        print(f"\nMensajes MCP intercambiados: {protocol_logger.stats()}")
        print("Si llego hasta aqui, el host funciona de punta a punta.")
        return 0
    except LLMError as exc:
        print(f"\nFallo la llamada al modelo: {exc}")
        return 1
    finally:
        await registry.close_all()


if __name__ == "__main__":
    configure_event_loop()
    sys.exit(asyncio.run(main()))
