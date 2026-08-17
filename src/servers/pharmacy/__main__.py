"""Entry point of the pharmacy MCP server.

    python -m servers.pharmacy            # from the src/ directory
    python src/servers/pharmacy/__main__.py

The host launches it as a child process and talks JSON-RPC over stdin/stdout.
Nothing is ever printed to stdout except protocol messages.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running the file directly, not only as a module.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.transport.stdio_server import configure_stderr_logging, serve_stdio  # noqa: E402
from servers.pharmacy.database import PharmacyDatabase  # noqa: E402
from servers.pharmacy.tools import build_server  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = PROJECT_ROOT / "data" / "pharmacy.db"
DEFAULT_SEED = PROJECT_ROOT / "data" / "pharmacy_seed.json"


def main() -> None:
    configure_stderr_logging(os.getenv("PHARMACY_LOG_LEVEL", "INFO"))

    # Overridable so the tests can point at a throwaway database.
    db = PharmacyDatabase(
        db_path=Path(os.getenv("PHARMACY_DB", DEFAULT_DB)),
        seed_path=Path(os.getenv("PHARMACY_SEED", DEFAULT_SEED)),
    )
    try:
        serve_stdio(build_server(db))
    finally:
        db.close()


if __name__ == "__main__":
    main()
