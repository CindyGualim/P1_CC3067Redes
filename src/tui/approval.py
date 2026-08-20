"""Confirmation dialog for tools that modify data.

Three HCI rules shape this screen:

* **Show what will happen, not that something will happen.** The arguments are
  listed in full, so the user confirms a concrete order and not an abstraction.
* **Make the safe choice the easy one.** "Cancelar" holds the focus, Escape
  cancels, and there is no default that commits by accident.
* **Name the consequence.** The title says what the tool does, taken from the
  server's own ``title`` and ``description``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


class ApprovalScreen(ModalScreen[bool]):
    """Returns True when the user authorizes the call."""

    BINDINGS = [
        ("escape", "deny", "Cancelar"),
        ("s", "approve", "Autorizar"),
        ("n", "deny", "Cancelar"),
    ]

    def __init__(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        title: Optional[str] = None,
        description: str = "",
    ) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.arguments = arguments
        self.tool_title = title or tool_name
        self.description = description

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-dialog"):
            yield Label(f"Confirmar: {self.tool_title}", id="approval-title")
            yield Label(self.tool_name, id="approval-tool")
            if self.description:
                yield Static(_wrap(self.description), classes="message-system")
            with VerticalScroll(id="approval-args"):
                yield Static(_render_arguments(self.arguments))
            with Horizontal(id="approval-buttons"):
                yield Button("Autorizar", variant="warning", id="approve")
                yield Button("Cancelar", variant="default", id="deny")

    def on_mount(self) -> None:
        # The reversible choice takes the focus, never the one that writes.
        self.query_one("#deny", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "approve")

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)


def _render_arguments(arguments: Dict[str, Any]) -> str:
    """One line per argument, nested values pretty-printed."""
    if not arguments:
        return "[dim]La herramienta no recibe argumentos.[/dim]"

    lines = []
    for key, value in arguments.items():
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, indent=2)
            lines.append(f"[bold]{key}[/bold]:")
            lines.extend(f"  {row}" for row in rendered.splitlines())
        else:
            lines.append(f"[bold]{key}[/bold]: {value}")
    return "\n".join(lines)


def _wrap(text: str, width: int = 66) -> str:
    import textwrap

    return "\n".join(textwrap.wrap(text, width=width)[:4])
