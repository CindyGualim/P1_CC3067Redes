"""Requirement 4: the official Filesystem and Git MCP servers, in one scenario.

    python scripts/demo_official_servers.py

Runs the scenario the project statement suggests — prepare a repository, create
a README, add it and commit it — driven through MCP against the two official
Anthropic servers, plus our own pharmacy server in the same session to show the
three coexisting under one host.

It is scripted rather than LLM-driven on purpose: this is the proof that the
transport and the client work against third-party servers, with no model in the
middle deciding what to call.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.logging_setup import setup_logging  # noqa: E402
from core.mcp.protocol_log import ProtocolLogger  # noqa: E402
from core.transport.stdio import configure_event_loop  # noqa: E402
from host.registry import ServerRegistry, load_server_configs  # noqa: E402
from host.workspace import Workspace  # noqa: E402

RULE = "=" * 78


def step(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


async def show(registry: ServerRegistry, tool: str, arguments: dict) -> None:
    print(f"\n-> {tool}({arguments})")
    result = await registry.call(tool, arguments)
    flag = "ERROR" if result.isError else "ok"
    text = result.as_text()
    print(f"<- [{flag}] {text[:600]}")


async def main() -> int:
    setup_logging(PROJECT_ROOT / "logs", level="WARNING", console=False)
    protocol_logger = ProtocolLogger(PROJECT_ROOT / "logs")

    workspace = Workspace(PROJECT_ROOT / "workspace")
    workspace.reset()  # repeatable runs
    repo = workspace.init_repo("demo-repo")

    configs = load_server_configs(
        PROJECT_ROOT / "config" / "servers.json", PROJECT_ROOT, workspace.root
    )
    registry = ServerRegistry(configs, protocol_logger=protocol_logger, request_timeout=120)

    step("1. Conectando los tres servidores MCP")
    print("La primera vez npx descarga el servidor Filesystem, puede tardar.")
    await registry.connect_all()
    for row in registry.describe():
        if row["status"] == "connected":
            print(f"  [ok]    {row['name']:<12} {row['server']} v{row['version']} "
                  f"| protocolo {row['protocol']} | {len(row['tools'])} herramientas")
        else:
            print(f"  [fallo] {row['name']:<12} {row['reason']}")

    if {"filesystem", "git"} - set(registry.clients):
        print("\nFaltan servidores oficiales, no se puede continuar.")
        await registry.close_all()
        return 1

    try:
        step("2. El servidor de archivos solo puede tocar el area de trabajo")
        await show(registry, "filesystem__list_allowed_directories", {})

        step("3. Estado inicial del repositorio")
        await show(registry, "git__git_status", {"repo_path": str(repo)})

        step("4. Crear el README con el servidor de archivos")
        await show(
            registry,
            "filesystem__write_file",
            {
                "path": str(repo / "README.md"),
                "content": (
                    "# Demo MCP\n\n"
                    "Archivo creado por el chatbot a traves del servidor MCP "
                    "Filesystem de Anthropic, y versionado con el servidor MCP Git.\n"
                ),
            },
        )
        await show(registry, "filesystem__list_directory", {"path": str(repo)})

        step("5. Agregarlo al repositorio y confirmar el commit")
        await show(registry, "git__git_add", {"repo_path": str(repo), "files": ["README.md"]})
        await show(registry, "git__git_diff_staged", {"repo_path": str(repo)})
        await show(
            registry,
            "git__git_commit",
            {"repo_path": str(repo), "message": "docs: add README created through MCP"},
        )

        step("6. Historial del repositorio")
        await show(registry, "git__git_log", {"repo_path": str(repo), "max_count": 5})

        step("7. El servidor propio sigue disponible en la misma sesion")
        await show(registry, "pharmacy__search_medicines", {"symptom": "fiebre", "limit": 2})

        step("Trafico MCP por servidor")
        per_server: dict[str, int] = {}
        for entry in protocol_logger.snapshot():
            per_server[entry.server] = per_server.get(entry.server, 0) + 1
        for name, count in sorted(per_server.items()):
            print(f"  {name:<12} {count} mensajes")
        print(f"\nTotales: {protocol_logger.stats()}")
        print(f"Log     : {protocol_logger.path}")
        print(f"Repo    : {repo}")
        return 0
    finally:
        await registry.close_all()


if __name__ == "__main__":
    configure_event_loop()
    sys.exit(asyncio.run(main()))
