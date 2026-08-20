"""Widgets of the chatbot interface.

The chat is the primary task and gets the left, wider column; the technical
panels (protocol log, servers, tools) are secondary and live in tabs on the
right, so the screen has two clear regions instead of one busy one.
"""

from __future__ import annotations

from typing import Any, Dict, List

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Static

from core.mcp.protocol_log import ProtocolEntry
from host.messages import ToolCall, ToolResult

#: Keeps memory bounded during a long session.
MAX_LOG_ROWS = 400


class ChatView(VerticalScroll):
    """Scrolling transcript of the conversation."""

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold #2dd4a7]Farmacia Vida[/bold #2dd4a7]\n"
            "Escriba sus sintomas, pregunte por un medicamento o consulte una receta.\n"
            "[dim]F1 ayuda  ·  Ctrl+R nueva conversacion  ·  Ctrl+Q salir[/dim]",
            classes="message message-system",
        )

    def _add(self, renderable: Any, classes: str) -> None:
        self.mount(Static(renderable, classes=classes))
        self.scroll_end(animate=False)

    def add_user(self, text: str) -> None:
        self._add(Text(text, no_wrap=False), "message message-user")

    def add_assistant(self, text: str) -> None:
        self._add(Text(text, no_wrap=False), "message message-assistant")

    def add_system(self, text: str) -> None:
        self._add(text, "message message-system")

    def add_error(self, text: str) -> None:
        self._add(Text(text, no_wrap=False), "message message-error")

    def add_tool_call(self, call: ToolCall) -> None:
        """Show the intent before the result, so waiting is explained.

        Built as a ``Text`` rather than with console markup: tool arguments are
        full of square brackets ("[MED-001]", JSON arrays) and Rich would read
        them as tags, swallowing the styling and printing the closing tag.
        """
        arguments = ", ".join(f"{key}={value!r}" for key, value in call.arguments.items())
        self._add(
            Text.assemble(
                ("→ ", "dim"),
                (call.name, "dim bold"),
                (f"({_clip(arguments, 70)})", "dim"),
            ),
            "message message-tool",
        )

    def add_tool_result(self, result: ToolResult) -> None:
        mark = ("sin exito", "#f87171") if result.is_error else ("listo", "#2dd4a7")
        first_line = result.text.splitlines()[0] if result.text else ""
        self._add(
            Text.assemble(
                ("← ", "dim"),
                mark,
                ("  " + _clip(first_line, 64), "dim"),
            ),
            "message message-tool",
        )

    def add_denied(self, call: ToolCall) -> None:
        self._add(
            Text.assemble(
                ("← operacion cancelada: ", "#f0b429"),
                (call.name, "dim"),
            ),
            "message message-tool",
        )


class ProtocolPanel(DataTable):
    """Live view of the MCP traffic (requirement 3: show the log)."""

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_columns("Hora", "Servidor", "", "Tipo", "Metodo", "ms")

    def append(self, entry: ProtocolEntry) -> None:
        arrow = "→" if entry.direction == "outgoing" else "←"
        kind_colour = {
            "request": "#7dd3fc",
            "response": "#2dd4a7",
            "error": "#f87171",
            "notification": "#c4b5fd",
        }.get(entry.kind, "white")

        self.add_row(
            Text(entry.timestamp[11:19], style="dim"),
            Text(entry.server, style="#8b9aa6"),
            Text(arrow, style="dim"),
            Text(entry.kind, style=kind_colour),
            Text(_clip(entry.method or f"id={entry.message_id}", 26)),
            Text(f"{entry.elapsed_ms:.0f}" if entry.elapsed_ms is not None else "", style="dim"),
        )
        if self.row_count > MAX_LOG_ROWS:
            self.remove_row(self.ordered_rows[0].key)
        self.scroll_end(animate=False)


class ServersPanel(DataTable):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.add_columns("Servidor", "Estado", "Version", "Protocolo", "Tools")

    def load(self, rows: List[Dict[str, Any]]) -> None:
        self.clear()
        for row in rows:
            if row["status"] == "connected":
                self.add_row(
                    Text(row["name"], style="bold"),
                    Text("conectado", style="#2dd4a7"),
                    Text(_clip(f"{row['server']} {row['version']}", 22)),
                    Text(row["protocol"] or "-", style="dim"),
                    str(len(row["tools"])),
                )
            else:
                self.add_row(
                    Text(row["name"], style="bold"),
                    Text("no disponible", style="#f87171"),
                    Text(_clip(row.get("reason", ""), 22), style="dim"),
                    Text("-", style="dim"),
                    "0",
                )


class ToolsPanel(DataTable):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.add_columns("Herramienta", "Confirma")

    def load(self, tools) -> None:
        self.clear()
        for tool in tools:
            self.add_row(
                Text(tool.qualified_name),
                # Amber again, and only here: these are the tools that write.
                Text("si", style="#f0b429") if tool.requires_approval else Text("no", style="dim"),
            )


def _clip(text: str, width: int) -> str:
    text = str(text).replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"
