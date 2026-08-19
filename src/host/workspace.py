"""Sandbox directory the official MCP servers are allowed to touch.

The Filesystem server is started with this folder as its only allowed root, so
the chatbot cannot read or write anything else on the machine — not the source
of this project, and not the user's documents.

It also creates the git repositories, and that deserves an explanation: the
official Git MCP server (``mcp-server-git``) exposes twelve tools and **none of
them initializes a repository** — `git_init` is not part of the published
package in any version. Since requirement 4 asks for a scenario that starts by
creating a repo, the host prepares the empty repository as part of the sandbox
and every actual git operation (add, commit, log, status, diff) goes through the
official server.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

DEFAULT_REPO = "demo-repo"


class WorkspaceError(RuntimeError):
    """The sandbox could not be prepared."""


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def ensure(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    # ------------------------------------------------------------------ #
    # Repositories
    # ------------------------------------------------------------------ #
    def repo_path(self, name: str = DEFAULT_REPO) -> Path:
        """Resolve a repository path, refusing anything outside the sandbox."""
        candidate = (self.root / name).resolve()
        if self.root not in candidate.parents and candidate != self.root:
            raise WorkspaceError(f"'{name}' queda fuera del area de trabajo permitida.")
        return candidate

    def init_repo(self, name: str = DEFAULT_REPO) -> Path:
        """Create an empty git repository if it does not exist yet."""
        path = self.repo_path(name)
        if (path / ".git").exists():
            logger.debug("Repository already present at %s", path)
            return path

        git = shutil.which("git")
        if git is None:
            raise WorkspaceError("No se encontro 'git' en el PATH del sistema.")

        path.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [git, "init", "-q", "-b", "main", str(path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise WorkspaceError(f"'git init' fallo: {result.stderr.strip()}")

        logger.info("Initialized repository at %s", path)
        return path

    def list_repos(self) -> List[str]:
        if not self.root.exists():
            return []
        return sorted(
            entry.name
            for entry in self.root.iterdir()
            if entry.is_dir() and (entry / ".git").exists()
        )

    def reset(self) -> None:
        """Empty the sandbox. Only ever touches paths under the root."""
        if not self.root.exists():
            return
        for entry in self.root.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        logger.info("Workspace cleared: %s", self.root)

    def describe(self) -> str:
        repos = self.list_repos()
        detail = ", ".join(repos) if repos else "sin repositorios"
        return f"{self.root} ({detail})"
