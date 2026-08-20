"""Allow ``python -m tui`` from the src/ directory."""

from pathlib import Path

from core.transport.stdio import configure_event_loop
from tui.app import run_tui

if __name__ == "__main__":
    configure_event_loop()
    run_tui(Path(__file__).resolve().parents[2])
