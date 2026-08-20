"""Interface tests driven by Textual's own pilot.

They run the real application headless: the servers connect, the panels fill and
the approval dialog behaves. The app is always started in offline mode so no
test ever needs an API key.
"""

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Button, Input, Static

from tui.app import PharmacyTUI
from tui.approval import ApprovalScreen
from tui.widgets import ChatView, ProtocolPanel, ServersPanel, ToolsPanel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIZE = (140, 45)


async def wait_until(predicate, pilot, timeout: float = 90.0) -> None:
    """Poll the UI until a condition holds, pumping Textual's message loop."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await pilot.pause()
        if predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("La condicion no se cumplio dentro del tiempo esperado")


def chat_text(app: PharmacyTUI) -> str:
    return "\n".join(
        str(widget.content) for widget in app.query_one(ChatView).query(Static)
    )


@pytest.fixture
def app():
    return PharmacyTUI(PROJECT_ROOT, offline=True)


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #
async def test_layout_has_chat_and_side_panels(app):
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app.query_one(ChatView)
        assert app.query_one(ProtocolPanel)
        assert app.query_one(ServersPanel)
        assert app.query_one(ToolsPanel)
        # The conversation is the primary task and gets the wider column.
        assert app.query_one("#chat-column").size.width > app.query_one("#side-column").size.width


async def test_input_is_locked_until_the_servers_answer(app):
    async with app.run_test(size=SIZE) as pilot:
        prompt = app.query_one("#prompt", Input)
        # Before the session worker finishes, typing must not be possible.
        assert prompt.disabled is True
        assert "Conectando" in prompt.placeholder

        await wait_until(lambda: not prompt.disabled, pilot)
        assert app.registry is not None


async def test_offline_mode_is_announced_and_allows_commands(app):
    async with app.run_test(size=SIZE) as pilot:
        prompt = app.query_one("#prompt", Input)
        await wait_until(lambda: not prompt.disabled, pilot)

        assert prompt.has_class("offline")
        assert "Modo offline" in chat_text(app)
        assert "/demo" in chat_text(app)


async def test_panels_are_populated_from_the_handshake(app):
    async with app.run_test(size=SIZE) as pilot:
        await wait_until(lambda: not app.query_one("#prompt", Input).disabled, pilot)

        assert app.query_one(ServersPanel).row_count >= 1
        assert app.query_one(ToolsPanel).row_count >= 7
        # Requirement 3: the MCP traffic is visible while it happens.
        assert app.query_one(ProtocolPanel).row_count > 0


async def test_status_bar_reports_the_session(app):
    async with app.run_test(size=SIZE) as pilot:
        await wait_until(lambda: not app.query_one("#prompt", Input).disabled, pilot)

        status = str(app.query_one("#status-bar", Static).content)
        assert "offline" in status
        assert "herramientas" in status


# --------------------------------------------------------------------------- #
# Interaction
# --------------------------------------------------------------------------- #
async def test_help_command_and_shortcut(app):
    async with app.run_test(size=SIZE) as pilot:
        prompt = app.query_one("#prompt", Input)
        await wait_until(lambda: not prompt.disabled, pilot)

        prompt.value = "/help"
        await pilot.press("enter")
        await wait_until(lambda: "Atajos" in chat_text(app), pilot, timeout=15)


async def test_unknown_command_is_reported(app):
    async with app.run_test(size=SIZE) as pilot:
        prompt = app.query_one("#prompt", Input)
        await wait_until(lambda: not prompt.disabled, pilot)

        prompt.value = "/inventar"
        await pilot.press("enter")
        await wait_until(lambda: "Comando desconocido" in chat_text(app), pilot, timeout=15)


async def test_a_message_without_model_explains_itself(app):
    async with app.run_test(size=SIZE) as pilot:
        prompt = app.query_one("#prompt", Input)
        await wait_until(lambda: not prompt.disabled, pilot)

        prompt.value = "hola"
        await pilot.press("enter")
        await wait_until(lambda: "El modelo no esta disponible" in chat_text(app), pilot, 15)


# --------------------------------------------------------------------------- #
# Approval dialog
# --------------------------------------------------------------------------- #
ORDER_ARGS = {
    "branch_id": "SUC-01",
    "customer_name": "Ana Lucia Morales",
    "items": [{"sku": "MED-001", "quantity": 2}],
}


async def push_approval(app, pilot) -> asyncio.Future:
    """Open the dialog and hand back the future its dismissal resolves.

    ``push_screen_wait`` only works inside a Textual worker, which is how the
    agent calls it; from a test the callback form is the equivalent.
    """
    future = asyncio.get_running_loop().create_future()
    screen = ApprovalScreen(
        "pharmacy__create_purchase_order",
        ORDER_ARGS,
        title="Generar orden de compra",
        description="Genera una orden y descuenta el inventario.",
    )
    app.push_screen(screen, lambda result: future.set_result(result))
    await wait_until(lambda: isinstance(app.screen, ApprovalScreen), pilot, timeout=15)
    return future


async def test_dialog_shows_every_argument(app):
    async with app.run_test(size=SIZE) as pilot:
        await wait_until(lambda: not app.query_one("#prompt", Input).disabled, pilot)
        decision = await push_approval(app, pilot)

        rendered = "\n".join(
            str(widget.content) for widget in app.screen.query(Static)
        )
        # The user must confirm a concrete order, not an abstract action.
        assert "SUC-01" in rendered
        assert "MED-001" in rendered
        assert "Generar orden de compra" in rendered

        await pilot.press("escape")
        assert await decision is False


async def test_cancel_holds_the_focus(app):
    """The reversible option is the one under the cursor when the modal opens."""
    async with app.run_test(size=SIZE) as pilot:
        await wait_until(lambda: not app.query_one("#prompt", Input).disabled, pilot)
        decision = await push_approval(app, pilot)

        assert app.screen.focused.id == "deny"

        await pilot.press("escape")
        assert await decision is False


async def test_approving_returns_true(app):
    async with app.run_test(size=SIZE) as pilot:
        await wait_until(lambda: not app.query_one("#prompt", Input).disabled, pilot)
        decision = await push_approval(app, pilot)

        await pilot.click(app.screen.query_one("#approve", Button))
        assert await decision is True


async def test_escape_denies(app):
    async with app.run_test(size=SIZE) as pilot:
        await wait_until(lambda: not app.query_one("#prompt", Input).disabled, pilot)
        decision = await push_approval(app, pilot)

        await pilot.press("escape")
        assert await decision is False
