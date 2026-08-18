"""Runtime configuration, read once at startup.

Secrets live in ``.env`` (git-ignored); ``.env.example`` documents the shape.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

#: Repository root: this file is <root>/src/core/config.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
LOGS_DIR = PROJECT_ROOT / "logs"
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_DIR = PROJECT_ROOT / "config"


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    gemini_model: str
    log_level: str
    request_timeout: float
    logs_dir: Path = LOGS_DIR

    @property
    def has_llm(self) -> bool:
        """The MCP layer is usable without a key; the chatbot is not."""
        return bool(self.gemini_api_key)


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    return Settings(
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        request_timeout=float(os.getenv("MCP_REQUEST_TIMEOUT", "60")),
    )
