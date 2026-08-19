"""Host side of MCP: the chatbot that coordinates the LLM and the servers."""

from host.agent import AgentEvent, ChatAgent, build_system_prompt
from host.conversation import Conversation
from host.registry import ServerConfig, ServerRegistry, load_server_configs
from host.workspace import Workspace, WorkspaceError

__all__ = [
    "AgentEvent",
    "ChatAgent",
    "build_system_prompt",
    "Conversation",
    "ServerConfig",
    "ServerRegistry",
    "load_server_configs",
    "Workspace",
    "WorkspaceError",
]
