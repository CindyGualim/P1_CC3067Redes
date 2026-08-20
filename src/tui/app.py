"""Textual interface for the pharmacy MCP chatbot.

Layout: the conversation owns the left column because it is the primary task,
and the protocol log, servers and tools live in tabs on the right, where they
can be consulted without interrupting the chat. Every long operation runs in a
Textual worker, so the interface never freezes while a server or the model is
answering.

    python src/main.py
    python src/main.py --offline    # no LLM: panels, /demo and commands
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, Static, TabbedContent, TabPane

from core.config import load_settings
from core.logging_setup import setup_logging
from core.mcp.protocol_log import ProtocolEntry, ProtocolLogger
from host.agent import AgentEvent, ChatAgent, build_system_prompt
from host.conversation import Conversation
from host.llm.base import LLMError
from host.llm.gemini import GeminiClient
from host.messages import ToolCall
from host.registry import RegisteredTool, ServerRegistry, load_server_configs
from host.workspace import Workspace, WorkspaceError
from tui.approval import ApprovalScreen
from tui.widgets import ChatView, ProtocolPanel, ServersPanel, ToolsPanel

logger = logging.getLogger(__name__)

HELP = """[bold]Comandos[/bold]
  /demo                 ejecuta un escenario de farmacia sin usar el modelo
  /workspace [nombre]   crea o lista los repositorios del area de trabajo
  /save                 guarda la conversacion en logs/conversacion.txt
  /reset                borra el contexto (tambien con Ctrl+R)
  /help                 esta ayuda

[bold]Atajos[/bold]
  F1 ayuda   ·   Ctrl+R nueva conversacion   ·   Ctrl+Q salir"""


class PharmacyTUI(App):
    CSS_PATH = "styles.tcss"
    TITLE = "Farmacia Vida"
    SUB_TITLE = "Chatbot MCP"

    BINDINGS = [
        ("ctrl+q", "quit", "Salir"),
        ("ctrl+r", "reset", "Nueva conversacion"),
        ("f1", "help", "Ayuda"),
    ]

    def __init__(self, project_root: Path, *, offline: bool = False) -> None:
        super().__init__()
        self.project_root = project_root
        self.offline = offline
        self.settings = load_settings()
        self.protocol_logger: Optional[ProtocolLogger] = None
        self.registry: Optional[ServerRegistry] = None
        self.workspace = Workspace(project_root / "workspace")
        self.agent: Optional[ChatAgent] = None
        self.busy = False

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="chat-column"):
                yield ChatView(id="chat-scroll")
                with Vertical(id="composer"):
                    yield Input(
                        placeholder="Conectando servidores MCP...",
                        id="prompt",
                        disabled=True,
                    )
            with Vertical(id="side-column"):
                with TabbedContent(id="side-tabs"):
                    with TabPane("Actividad MCP", id="tab-protocol"):
                        yield ProtocolPanel(id="protocol-table")
                    with TabPane("Servidores", id="tab-servers"):
                        yield ServersPanel(id="servers-table")
                    with TabPane("Herramientas", id="tab-tools"):
                        yield ToolsPanel(id="tools-table")
                yield Static("", id="status-bar")
        yield Footer()

    # ------------------------------------------------------------------ #
    # Startup
    # ------------------------------------------------------------------ #
    def on_mount(self) -> None:
        setup_logging(self.settings.logs_dir, level=self.settings.log_level, console=False)
        self.protocol_logger = ProtocolLogger(self.settings.logs_dir)
        self.protocol_logger.subscribe(self._on_protocol_entry)
        self.start_session()

    @work(exclusive=True)
    async def start_session(self) -> None:
        chat = self.query_one(ChatView)
        self.workspace.ensure()
        try:
            self.workspace.init_repo()
        except WorkspaceError as exc:
            chat.add_error(f"No se pudo preparar el area de trabajo: {exc}")

        configs = load_server_configs(
            self.project_root / "config" / "servers.json",
            self.project_root,
            self.workspace.root,
        )
        self.registry = ServerRegistry(
            configs,
            protocol_logger=self.protocol_logger,
            request_timeout=self.settings.request_timeout,
        )
        await self.registry.connect_all()

        self.query_one(ServersPanel).load(self.registry.describe())
        self.query_one(ToolsPanel).load(self.registry.tools)

        for name, reason in self.registry.failures.items():
            chat.add_error(f"El servidor '{name}' no esta disponible: {reason}")

        if not self.registry.clients:
            chat.add_error("Ningun servidor MCP respondio. Revise config/servers.json.")
            self._set_status("sin servidores")
            return

        self._start_agent(chat)

    def _start_agent(self, chat: ChatView) -> None:
        prompt = self.query_one("#prompt", Input)

        if self.offline or not self.settings.has_llm:
            reason = (
                "Modo offline solicitado."
                if self.offline
                else "No hay GEMINI_API_KEY en .env, el modelo no esta disponible."
            )
            chat.add_system(
                f"[#f0b429]{reason}[/#f0b429]\n"
                "Los servidores MCP si estan conectados: use [bold]/demo[/bold] para "
                "ver un escenario completo, o revise las pestanias de la derecha."
            )
            prompt.placeholder = "Modo offline: solo comandos (/demo, /help)"
            prompt.add_class("offline")
            prompt.disabled = False
            prompt.focus()
            self._set_status("offline")
            return

        try:
            llm = GeminiClient(self.settings.gemini_api_key, self.settings.gemini_model)
        except LLMError as exc:
            chat.add_error(str(exc))
            self._set_status("sin modelo")
            return

        self.agent = ChatAgent(
            llm,
            self.registry,
            Conversation(system_instruction=build_system_prompt(self.registry, self.workspace)),
            approval=self.request_approval,
            on_event=self._on_agent_event,
        )
        chat.add_system("Listo. Cuenteme en que puedo ayudarle.")
        prompt.placeholder = "Escriba su mensaje y presione Enter"
        prompt.disabled = False
        prompt.focus()
        self._set_status("listo")

    def _set_status(self, state: str) -> None:
        servers = len(self.registry.clients) if self.registry else 0
        tools = len(self.registry.tools) if self.registry else 0
        model = self.settings.gemini_model if self.agent else "sin modelo"
        self.query_one("#status-bar", Static).update(
            f"{state}  ·  {servers} servidor(es)  ·  {tools} herramientas  ·  {model}"
        )

    # ------------------------------------------------------------------ #
    # Events from the protocol and the agent
    # ------------------------------------------------------------------ #
    def _on_protocol_entry(self, entry: ProtocolEntry) -> None:
        """Called from the event loop whenever a JSON-RPC message is recorded."""
        try:
            self.call_later(self.query_one(ProtocolPanel).append, entry)
        except Exception:  # the panel may not be mounted yet during startup
            logger.debug("Protocol entry arrived before the panel was ready")

    def _on_agent_event(self, event: AgentEvent) -> None:
        chat = self.query_one(ChatView)
        if event.kind == "tool_call":
            chat.add_tool_call(event.tool_call)
        elif event.kind == "tool_result":
            chat.add_tool_result(event.result)
        elif event.kind == "tool_denied":
            chat.add_denied(event.tool_call)
        elif event.kind == "error":
            chat.add_error(event.text)

    async def request_approval(self, call: ToolCall, registered: RegisteredTool) -> bool:
        """Human in the loop: nothing that writes runs without a yes."""
        return await self.push_screen_wait(
            ApprovalScreen(
                call.name,
                call.arguments,
                title=registered.tool.title or registered.tool.name,
                description=registered.tool.description or "",
            )
        )

    # ------------------------------------------------------------------ #
    # Input
    # ------------------------------------------------------------------ #
    @on(Input.Submitted, "#prompt")
    def on_submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or self.busy:
            return
        event.input.value = ""

        if text.startswith("/"):
            self.run_command(text)
            return

        if self.agent is None:
            self.query_one(ChatView).add_error(
                "El modelo no esta disponible. Use /demo para ver el escenario sin LLM."
            )
            return

        self.query_one(ChatView).add_user(text)
        self.process_message(text)

    @work(exclusive=True)
    async def process_message(self, text: str) -> None:
        self._set_busy(True, "pensando...")
        try:
            answer = await self.agent.send(text)
            self.query_one(ChatView).add_assistant(answer)
        finally:
            self._set_busy(False, "listo")

    def _set_busy(self, busy: bool, state: str) -> None:
        self.busy = busy
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = busy
        if not busy:
            prompt.focus()
        self._set_status(state)

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    @work(exclusive=True)
    async def run_command(self, line: str) -> None:
        chat = self.query_one(ChatView)
        parts = line.split()
        command = parts[0].lower()

        if command == "/help":
            chat.add_system(HELP)
        elif command == "/reset":
            self.action_reset()
        elif command == "/save":
            target = self.project_root / "logs" / "conversacion.txt"
            transcript = self.agent.conversation.transcript() if self.agent else ""
            target.write_text(transcript, encoding="utf-8")
            chat.add_system(f"Conversacion guardada en {target}")
        elif command == "/workspace":
            if len(parts) > 1:
                try:
                    path = self.workspace.init_repo(parts[1])
                    chat.add_system(f"Repositorio listo en {path}")
                except WorkspaceError as exc:
                    chat.add_error(str(exc))
            chat.add_system(f"Area de trabajo: {self.workspace.describe()}")
        elif command == "/demo":
            await self.run_demo()
        else:
            chat.add_error(f"Comando desconocido: {command}. Use /help.")

    async def run_demo(self) -> None:
        """Scripted pharmacy scenario, so the UI can be shown without an LLM.

        The order at the end buys an over-the-counter medicine on purpose: the
        demo can be run as many times as needed during a presentation without
        exhausting a seeded prescription. The prescription-backed purchase lives
        in ``scripts/demo_pharmacy.py``.
        """
        chat = self.query_one(ChatView)
        if self.registry is None or "pharmacy" not in self.registry.clients:
            chat.add_error("El servidor de farmacia no esta conectado.")
            return

        self._set_busy(True, "ejecutando demo...")
        try:
            chat.add_system(
                "Escenario: sintoma, ficha del medicamento, verificacion de receta "
                "y orden de compra."
            )
            steps = [
                ("pharmacy__search_medicines", {"symptom": "dolor de cabeza", "limit": 3}),
                ("pharmacy__get_medicine_details", {"sku": "MED-001"}),
                ("pharmacy__verify_prescription", {"folio": "RX-2026-0005"}),
            ]
            for name, arguments in steps:
                chat.add_tool_call(ToolCall(name=name, arguments=arguments))
                result = await self.registry.call(name, arguments)
                chat.add_assistant(result.as_text())

            order_name = "pharmacy__create_purchase_order"
            order_args = {
                "branch_id": "SUC-01",
                "customer_name": "Ana Lucia Morales",
                "items": [{"sku": "MED-001", "quantity": 1}],
            }
            call = ToolCall(name=order_name, arguments=order_args)
            registered = self.registry.find(order_name)
            chat.add_tool_call(call)

            if registered and registered.requires_approval:
                if not await self.request_approval(call, registered):
                    chat.add_denied(call)
                    return

            result = await self.registry.call(order_name, order_args)
            if result.isError:
                chat.add_error(result.as_text())
            else:
                chat.add_assistant(result.as_text())
        finally:
            self._set_busy(False, "listo")

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #
    def action_help(self) -> None:
        self.query_one(ChatView).add_system(HELP)

    def action_reset(self) -> None:
        chat = self.query_one(ChatView)
        if self.agent:
            self.agent.conversation.reset()
        chat.add_system("Contexto borrado, empezamos de nuevo.")

    async def on_unmount(self) -> None:
        if self.registry:
            await self.registry.close_all()


def run_tui(project_root: Path, offline: bool = False) -> None:
    PharmacyTUI(project_root, offline=offline).run()
