"""Render the interface headless and save it as SVG, for the report.

    python scripts/screenshot_tui.py

Runs the app in offline mode, executes the scripted pharmacy scenario, and
captures two frames: the approval dialog and the finished conversation.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from textual.widgets import Button, Input  # noqa: E402

from core.transport.stdio import configure_event_loop  # noqa: E402
from tui.app import PharmacyTUI  # noqa: E402
from tui.approval import ApprovalScreen  # noqa: E402

DOCS = PROJECT_ROOT / "docs" / "img"
SIZE = (150, 46)


async def wait_until(predicate, pilot, timeout: float = 120.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await pilot.pause()
        if predicate():
            return
        await asyncio.sleep(0.1)
    raise TimeoutError("La interfaz no llego al estado esperado")


async def main() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    app = PharmacyTUI(PROJECT_ROOT, offline=True)

    async with app.run_test(size=SIZE) as pilot:
        prompt = app.query_one("#prompt", Input)
        await wait_until(lambda: not prompt.disabled, pilot)

        prompt.value = "/demo"
        await pilot.press("enter")

        # The scenario stops on the confirmation dialog: capture it.
        await wait_until(lambda: isinstance(app.screen, ApprovalScreen), pilot)
        await pilot.pause()
        app.save_screenshot(str(DOCS / "tui-confirmacion.svg"))
        print("guardado:", DOCS / "tui-confirmacion.svg")

        await pilot.click(app.screen.query_one("#approve", Button))
        await wait_until(lambda: not app.busy, pilot)
        await pilot.pause()
        app.save_screenshot(str(DOCS / "tui-conversacion.svg"))
        print("guardado:", DOCS / "tui-conversacion.svg")


if __name__ == "__main__":
    configure_event_loop()
    asyncio.run(main())
