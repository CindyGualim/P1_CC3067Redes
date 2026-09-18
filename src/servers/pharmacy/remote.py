"""Cloud Run compatible HTTP entry point for the Pharmacy MCP server."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.transport.http_server import serve_http  # noqa: E402
from servers.pharmacy.database import PharmacyDatabase  # noqa: E402
from servers.pharmacy.tools import build_server  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SEED = PROJECT_ROOT / "data" / "pharmacy_seed.json"


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("PHARMACY_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(levelname)s | %(name)s | %(message)s",
    )
    token = os.getenv("MCP_AUTH_TOKEN", "").strip()
    if not token and os.getenv("MCP_ALLOW_INSECURE", "").lower() not in {"1", "true", "yes"}:
        raise RuntimeError(
            "MCP_AUTH_TOKEN is required. Set MCP_ALLOW_INSECURE=true only for local tests."
        )

    db_path = Path(os.getenv("PHARMACY_DB", "/tmp/pharmacy.db"))
    seed_path = Path(os.getenv("PHARMACY_SEED", DEFAULT_SEED))
    database = PharmacyDatabase(db_path=db_path, seed_path=seed_path)
    try:
        serve_http(
            build_server(database),
            os.getenv("HOST", "0.0.0.0"),
            int(os.getenv("PORT", "8080")),
            auth_token=token,
            allowed_origin=os.getenv("MCP_ALLOWED_ORIGIN") or None,
            max_body_bytes=int(os.getenv("MCP_MAX_BODY_BYTES", str(1024 * 1024))),
        )
    finally:
        database.close()


if __name__ == "__main__":
    main()

