"""Application logging.

Kept separate from the protocol log: this one is for diagnostics (stack traces,
subprocess stderr), the other one is the auditable record of the MCP traffic.

The console handler is opt-in because the TUI owns the terminal; when the TUI is
running, logs only go to the file.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    *,
    console: bool = True,
    filename: str = "app.log",
) -> logging.Logger:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.FileHandler(log_dir / filename, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(file_handler)

    if console:
        try:
            from rich.logging import RichHandler

            console_handler: logging.Handler = RichHandler(
                rich_tracebacks=True, show_path=False
            )
            console_handler.setFormatter(logging.Formatter("%(message)s"))
        except ImportError:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(console_handler)

    return root


def get_logger(name: Optional[str] = None) -> logging.Logger:
    return logging.getLogger(name)
