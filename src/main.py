"""Console chatbot for the pharmacy MCP host.

    python src/main.py             # chat (needs GEMINI_API_KEY)
    python src/main.py --offline   # connect the servers and inspect them, no LLM

This REPL covers requirements 1, 2 and 3: it talks to Gemini at the API level,
keeps the conversation context, and shows the MCP log with /log. The Textual TUI
of the last commit replaces the presentation layer, not this logic.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402

from core.config import load_settings  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from core.mcp.protocol_log import ProtocolLogger  # noqa: E402
from core.transport.stdio import configure_event_loop  # noqa: E402
from host.agent import AgentEvent, ChatAgent, build_system_prompt  # noqa: E402
from host.conversation import Conversation  # noqa: E402
from host.llm.base import LLMError  # noqa: E402
from host.llm.gemini import GeminiClient  # noqa: E402
from host.messages import ToolCall  # noqa: E402
from host.registry import RegisteredTool, ServerRegistry, load_server_configs  # noqa: E402
from host.workspace import Workspace, WorkspaceError  # noqa: E402

console = Console()

HELP = """[bold]Comandos[/bold]
  /servers   estado de los servidores MCP conectados
  /tools     herramientas disponibles y a que servidor pertenecen
  /log [n]   ultimos n mensajes JSON-RPC intercambiados (por defecto 15)
  /workspace          repositorios del area de trabajo
  /workspace <nombre> crea un repositorio git vacio para que lo use el chatbot
  /save      guarda la conversacion en logs/conversacion.txt
  /reset     borra el contexto y empieza una sesion nueva
  /help      esta ayuda
  /salir     terminar"""


def render_event(event: AgentEvent) -> None:
    """Show the agent's progress while it works."""
    if event.kind == "tool_call":
        call = event.tool_call
        console.print(f"[dim]  -> {call.name}({_short(call.arguments)})[/dim]")
    elif event.kind == "tool_result":
        result = event.result
        mark = "[red]error[/red]" if result.is_error else "[green]ok[/green]"
        console.print(f"[dim]  <- {mark}: {_short(result.text)}[/dim]")
    elif event.kind == "tool_denied":
        console.print("[yellow]  <- operacion cancelada por el usuario[/yellow]")
    elif event.kind == "error":
        console.print(f"[red]{event.text}[/red]")


def _short(value: object, width: int = 90) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= width else text[: width - 3] + "..."


async def ask_approval(call: ToolCall, registered: RegisteredTool) -> bool:
    """Confirmation prompt for tools that modify data."""
    table = Table(show_header=False, box=None, pad_edge=False)
    for key, value in call.arguments.items():
        table.add_row(f"[cyan]{key}[/cyan]", str(value))

    console.print()
    console.print(
        Panel(
            table,
            title=f"[bold yellow]Confirmar {registered.tool.title or call.tool}[/bold yellow]",
            subtitle=f"[dim]{call.name}[/dim]",
            border_style="yellow",
        )
    )
    # input() blocks, so it runs off the event loop: the MCP reader tasks must
    # keep draining their pipes while the user decides.
    answer = await asyncio.to_thread(input, "Autorizar esta operacion? [s/N]: ")
    return answer.strip().lower() in {"s", "si", "sí", "y", "yes"}


def show_servers(registry: ServerRegistry) -> None:
    table = Table(title="Servidores MCP")
    table.add_column("Nombre", style="cyan")
    table.add_column("Estado")
    table.add_column("Version")
    table.add_column("Protocolo")
    table.add_column("Herramientas", justify="right")

    for row in registry.describe():
        if row["status"] == "connected":
            table.add_row(
                row["name"],
                "[green]conectado[/green]",
                f"{row['server']} v{row['version']}",
                row["protocol"] or "-",
                str(len(row["tools"])),
            )
        else:
            table.add_row(row["name"], "[red]fallo[/red]", _short(row["reason"], 40), "-", "0")
    console.print(table)


def show_tools(registry: ServerRegistry) -> None:
    table = Table(title="Herramientas expuestas al LLM")
    table.add_column("Nombre calificado", style="cyan")
    table.add_column("Confirma", justify="center")
    table.add_column("Descripcion")

    for tool in registry.tools:
        table.add_row(
            tool.qualified_name,
            "[yellow]si[/yellow]" if tool.requires_approval else "no",
            _short(tool.tool.description, 70),
        )
    console.print(table)


def show_log(protocol_logger: ProtocolLogger, count: int = 15) -> None:
    table = Table(title=f"Log MCP (ultimos {count}) - {protocol_logger.path.name}")
    table.add_column("Hora", style="dim")
    table.add_column("Servidor", style="cyan")
    table.add_column("Dir", justify="center")
    table.add_column("Tipo")
    table.add_column("Metodo / id")
    table.add_column("ms", justify="right")

    for entry in protocol_logger.snapshot()[-count:]:
        table.add_row(
            entry.timestamp[11:23],
            entry.server,
            "-->" if entry.direction == "outgoing" else "<--",
            entry.kind,
            entry.method or f"id={entry.message_id}",
            f"{entry.elapsed_ms:.0f}" if entry.elapsed_ms is not None else "",
        )
    console.print(table)
    console.print(f"[dim]Totales: {protocol_logger.stats()}[/dim]")


async def handle_command(
    line: str,
    registry: ServerRegistry,
    conversation: Conversation,
    plog: ProtocolLogger,
    workspace: Workspace,
) -> bool:
    """Return False when the user asked to quit."""
    parts = line.split()
    command = parts[0].lower()

    if command in {"/salir", "/quit", "/exit"}:
        return False
    if command == "/help":
        console.print(HELP)
    elif command == "/servers":
        show_servers(registry)
    elif command == "/tools":
        show_tools(registry)
    elif command == "/log":
        count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 15
        show_log(plog, count)
    elif command == "/workspace":
        # The official Git server has no git_init tool, so new repositories are
        # created here, by the host, inside the sandbox it controls.
        if len(parts) > 1:
            try:
                path = workspace.init_repo(parts[1])
                console.print(f"[green]Repositorio listo en {path}[/green]")
            except WorkspaceError as exc:
                console.print(f"[red]{exc}[/red]")
        console.print(f"[dim]Area de trabajo: {workspace.describe()}[/dim]")
    elif command == "/reset":
        conversation.reset()
        console.print("[green]Contexto borrado.[/green]")
    elif command == "/save":
        target = PROJECT_ROOT / "logs" / "conversacion.txt"
        target.write_text(conversation.transcript(), encoding="utf-8")
        console.print(f"[green]Conversacion guardada en {target}[/green]")
    else:
        console.print(f"[red]Comando desconocido: {command}[/red]. Use /help.")
    return True


async def run(offline: bool) -> int:
    settings = load_settings()
    setup_logging(settings.logs_dir, level=settings.log_level, console=False)
    protocol_logger = ProtocolLogger(settings.logs_dir)

    workspace = Workspace(PROJECT_ROOT / "workspace")
    workspace.ensure()
    workspace.init_repo()  # the sandbox always has one repository ready to use

    configs = load_server_configs(
        PROJECT_ROOT / "config" / "servers.json", PROJECT_ROOT, workspace.root
    )
    registry = ServerRegistry(
        configs, protocol_logger=protocol_logger, request_timeout=settings.request_timeout
    )

    console.print("[dim]Conectando servidores MCP...[/dim]")
    await registry.connect_all()
    show_servers(registry)

    if not registry.clients:
        console.print("[red]Ningun servidor MCP disponible. Revise config/servers.json[/red]")
        return 1

    try:
        if offline:
            show_tools(registry)
            console.print(
                "\n[yellow]Modo offline: los servidores responden, pero no hay LLM.[/yellow]"
            )
            return 0

        try:
            llm = GeminiClient(settings.gemini_api_key, settings.gemini_model)
        except LLMError as exc:
            console.print(f"[red]{exc}[/red]")
            console.print("[dim]Mientras tanto puede usar: python src/main.py --offline[/dim]")
            return 1

        conversation = Conversation(
            system_instruction=build_system_prompt(registry, workspace)
        )
        agent = ChatAgent(
            llm,
            registry,
            conversation,
            approval=ask_approval,
            on_event=render_event,
        )

        console.print(
            Panel(
                "Asistente de [bold]Farmacia Vida[/bold]. Pregunte por sintomas, "
                "medicamentos, existencias o recetas.\nEscriba [cyan]/help[/cyan] para "
                "ver los comandos.",
                title=f"[bold green]Chatbot MCP[/bold green] - {llm.model}",
                border_style="green",
            )
        )

        while True:
            try:
                line = (await asyncio.to_thread(input, "\nUsted: ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line.startswith("/"):
                if not await handle_command(
                    line, registry, conversation, protocol_logger, workspace
                ):
                    break
                continue

            answer = await agent.send(line)
            console.print(Panel(answer, title="[bold cyan]Asistente[/bold cyan]",
                                border_style="cyan"))
        return 0
    finally:
        console.print("\n[dim]Cerrando servidores MCP...[/dim]")
        await registry.close_all()
        console.print(f"[dim]Log del protocolo: {protocol_logger.path}[/dim]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Chatbot MCP de Farmacia Vida")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Conecta los servidores MCP y muestra sus herramientas, sin usar el LLM.",
    )
    args = parser.parse_args()

    configure_event_loop()
    sys.exit(asyncio.run(run(args.offline)))


if __name__ == "__main__":
    main()
