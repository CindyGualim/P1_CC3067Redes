"""Requirement 4: the official Filesystem and Git MCP servers.

These exercise our hand-written client against third-party servers, which is the
real proof that the protocol implementation follows the spec and not just our
own conventions. They are skipped when Node or the git server package is not
installed, so the suite still runs on a bare machine.
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core.mcp.protocol_log import ProtocolLogger
from host.agent import build_system_prompt
from host.registry import (
    ServerConfig,
    ServerRegistry,
    executable_missing,
    resolve_executable,
)
from host.workspace import Workspace, WorkspaceError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NPX = shutil.which("npx.cmd") or shutil.which("npx")
HAS_GIT_SERVER = importlib.util.find_spec("mcp_server_git") is not None
HAS_GIT = shutil.which("git") is not None

needs_node = pytest.mark.skipif(NPX is None, reason="Node/npx no esta instalado")
needs_git_server = pytest.mark.skipif(
    not (HAS_GIT_SERVER and HAS_GIT), reason="mcp-server-git o git no estan instalados"
)


# --------------------------------------------------------------------------- #
# Workspace sandbox (no servers needed)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not HAS_GIT, reason="git no esta instalado")
def test_init_repo_is_idempotent(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.ensure()

    first = workspace.init_repo("demo")
    second = workspace.init_repo("demo")

    assert first == second
    assert (first / ".git").is_dir()
    assert workspace.list_repos() == ["demo"]


def test_repo_path_cannot_escape_the_sandbox(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.ensure()
    with pytest.raises(WorkspaceError):
        workspace.repo_path("../fuera")


@pytest.mark.skipif(not HAS_GIT, reason="git no esta instalado")
def test_reset_empties_the_sandbox(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.ensure()
    workspace.init_repo("demo")

    workspace.reset()

    assert workspace.list_repos() == []
    assert workspace.root.exists()


def test_executable_resolution(tmp_path):
    assert executable_missing(["definitivamente-no-existe-xyz"]) is True
    # An absolute path that exists is left untouched.
    assert resolve_executable([sys.executable, "-V"]) == [sys.executable, "-V"]


@needs_node
def test_npx_is_resolved_to_a_real_program():
    """On Windows the shim is npx.cmd; create_subprocess_exec needs the full name."""
    resolved = resolve_executable(["npx", "-y"])
    assert Path(resolved[0]).exists()
    assert executable_missing(resolved) is False


# --------------------------------------------------------------------------- #
# Live sessions against the official servers
# --------------------------------------------------------------------------- #
@pytest.fixture
def workspace(tmp_path):
    space = Workspace(tmp_path / "workspace")
    space.ensure()
    return space


def official_configs(workspace: Workspace):
    return [
        ServerConfig(
            name="filesystem",
            command=resolve_executable(
                ["npx", "-y", "@modelcontextprotocol/server-filesystem", str(workspace.root)]
            ),
        ),
        ServerConfig(
            name="git", command=[sys.executable, "-m", "mcp_server_git"]
        ),
    ]


@pytest.fixture
async def official(workspace, tmp_path):
    registry = ServerRegistry(
        official_configs(workspace),
        protocol_logger=ProtocolLogger(tmp_path / "logs"),
        request_timeout=120,
    )
    await registry.connect_all()
    yield registry, workspace
    await registry.close_all()


@needs_node
@needs_git_server
async def test_both_official_servers_complete_the_handshake(official):
    registry, _ = official
    assert registry.failures == {}
    assert set(registry.clients) == {"filesystem", "git"}

    filesystem = registry.clients["filesystem"]
    assert filesystem.server_info.name == "secure-filesystem-server"
    assert registry.clients["git"].server_info.name == "mcp-git"

    # Tool names collide across servers only until they are qualified.
    names = [tool.qualified_name for tool in registry.tools]
    assert "filesystem__write_file" in names
    assert "git__git_commit" in names
    assert len(names) == len(set(names))


@needs_node
@needs_git_server
async def test_approval_gate_follows_the_official_annotations(official):
    """The third-party servers declare readOnlyHint, so the gate just works."""
    registry, _ = official
    approval = {tool.qualified_name: tool.requires_approval for tool in registry.tools}

    assert approval["filesystem__read_file"] is False
    assert approval["filesystem__write_file"] is True
    assert approval["git__git_log"] is False
    assert approval["git__git_commit"] is True


@needs_node
@needs_git_server
async def test_filesystem_server_refuses_paths_outside_the_sandbox(official, tmp_path):
    registry, _ = official
    outside = tmp_path / "secreto.txt"
    outside.write_text("no deberia leerse", encoding="utf-8")

    result = await registry.call("filesystem__read_text_file", {"path": str(outside)})

    assert result.isError is True
    assert "outside allowed directories" in result.as_text().lower()


@needs_node
@needs_git_server
async def test_readme_scenario_end_to_end(official):
    """Prepare a repo, write a README through MCP, stage it and commit it."""
    registry, workspace = official
    repo = workspace.init_repo("demo-repo")

    written = await registry.call(
        "filesystem__write_file",
        {"path": str(repo / "README.md"), "content": "# Demo MCP\n"},
    )
    assert written.isError is False
    assert (repo / "README.md").exists()

    staged = await registry.call(
        "git__git_add", {"repo_path": str(repo), "files": ["README.md"]}
    )
    assert staged.isError is False

    committed = await registry.call(
        "git__git_commit",
        {"repo_path": str(repo), "message": "docs: add README created through MCP"},
    )
    assert committed.isError is False

    history = await registry.call("git__git_log", {"repo_path": str(repo), "max_count": 5})
    assert "README created through MCP" in history.as_text()

    # And the repository is a real one for git itself, not only for the server.
    log = subprocess.run(
        [shutil.which("git"), "-C", str(repo), "log", "--oneline"],
        capture_output=True,
        text=True,
    )
    assert "add README created through MCP" in log.stdout


@needs_node
@needs_git_server
async def test_system_prompt_announces_the_sandbox(official):
    registry, workspace = official
    workspace.init_repo("demo-repo")

    prompt = build_system_prompt(registry, workspace)

    assert str(workspace.root) in prompt
    assert "demo-repo" in prompt
    # The model must be told the git server cannot create repositories.
    assert "no puede crear repositorios" in prompt
