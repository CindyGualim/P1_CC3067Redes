"""Guard for the hard rule of the project statement.

The protocol must be implemented by hand: no FastMCP, no `mcp` SDK. The package
`mcp` does end up installed, because the official Git MCP server depends on it,
but that server runs as a separate process and our code must never import it.

This test fails the moment somebody takes the shortcut.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
FORBIDDEN_ROOTS = {"mcp", "fastmcp", "mcp_server_git"}

PYTHON_FILES = sorted(SRC.rglob("*.py"))


def imported_modules(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module


def test_the_source_tree_is_not_empty():
    assert len(PYTHON_FILES) > 10


@pytest.mark.parametrize("path", PYTHON_FILES, ids=lambda p: p.name)
def test_no_mcp_sdk_is_imported(path):
    for module in imported_modules(path):
        root = module.split(".")[0]
        assert root not in FORBIDDEN_ROOTS, (
            f"{path.relative_to(SRC)} importa '{module}'. El protocolo debe "
            "implementarse a mano, sin SDK de MCP."
        )
