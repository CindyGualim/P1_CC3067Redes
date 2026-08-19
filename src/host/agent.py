"""The agent: turns a user message into an answer, running MCP tools on the way.

This is the loop the whole project revolves around:

    user text
      -> LLM decides: answer, or call one or more tools
      -> tools that write ask the user first
      -> results go back into the history
      -> LLM sees the results and either calls more tools or answers

The loop is bounded by ``max_steps`` so a model that keeps asking for tools
cannot spin forever.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from host.conversation import Conversation
from host.llm.base import LLMClient, LLMError
from host.messages import LLMTurn, ToolCall, ToolResult
from host.registry import RegisteredTool, ServerRegistry
from host.workspace import Workspace

logger = logging.getLogger(__name__)

BASE_SYSTEM_PROMPT = """Eres el asistente virtual de la cadena Farmacia Vida, en Guatemala.
Atiendes a clientes por chat, en espaniol, con un tono cordial y claro.

Reglas de trabajo:
1. Nunca inventes medicamentos, precios, existencias ni recetas. Toda esa
   informacion se obtiene exclusivamente con las herramientas disponibles.
2. No diagnosticas ni sustituyes a un profesional de la salud. Cuando el usuario
   describe sintomas, busca opciones con las herramientas, explica para que sirve
   cada una y recomienda consultar a un medico o farmaceutico.
3. Antes de vender un medicamento que requiere receta, verifica el folio con la
   herramienta correspondiente y explica el resultado.
4. Antes de generar una orden de compra, confirma con el usuario los
   medicamentos, las cantidades y la sucursal.
5. Si una herramienta devuelve un error, explicale al usuario que paso en
   lenguaje sencillo y ofrece la alternativa que el propio mensaje sugiera.
6. Menciona el SKU entre corchetes cuando hables de un medicamento concreto, y
   los precios en quetzales.
7. Si la pregunta no tiene que ver con la farmacia, responde con tu conocimiento
   general sin usar herramientas."""


@dataclass
class AgentEvent:
    """Something worth showing in the UI while the turn is being processed."""

    kind: str  # thinking | tool_call | tool_result | tool_denied | answer | error
    text: str = ""
    tool_call: Optional[ToolCall] = None
    result: Optional[ToolResult] = None
    data: Dict[str, Any] = field(default_factory=dict)


EventSink = Callable[[AgentEvent], None]
ApprovalCallback = Callable[[ToolCall, RegisteredTool], Awaitable[bool]]


WORKSPACE_RULES = """
Ademas de la farmacia tienes acceso a los servidores oficiales de archivos y de
Git, restringidos al directorio de trabajo {workspace}.

8. Todas las rutas que envies a esas herramientas deben ser absolutas y estar
   dentro de ese directorio. El servidor de archivos rechaza cualquier otra.
9. El servidor de Git no puede crear repositorios: trabaja sobre los que ya
   existen en el directorio de trabajo. Los repositorios disponibles son: {repos}.
   Si el usuario pide uno nuevo, indicale que lo cree con el comando /workspace.
10. Para agregar un archivo a un repositorio: escribelo con el servidor de
    archivos, agregalo con git_add y luego confirma con git_commit."""


def build_system_prompt(
    registry: ServerRegistry, workspace: Optional[Workspace] = None
) -> str:
    """Base rules plus whatever each server declared in its handshake.

    Using the ``instructions`` field of ``initialize`` means a new server can
    teach the assistant how to use it without touching the host's code.
    """
    sections = [BASE_SYSTEM_PROMPT]

    if workspace is not None and {"filesystem", "git"} & set(registry.clients):
        repos = ", ".join(workspace.list_repos()) or "ninguno todavia"
        sections.append(
            WORKSPACE_RULES.format(workspace=workspace.root, repos=repos)
        )

    instructions = registry.instructions()
    if instructions:
        sections.append("\nInstrucciones de los servidores conectados:")
        for name, text in instructions.items():
            sections.append(f"- [{name}] {text}")
    return "\n".join(sections)


class ChatAgent:
    def __init__(
        self,
        llm: LLMClient,
        registry: ServerRegistry,
        conversation: Conversation,
        *,
        approval: Optional[ApprovalCallback] = None,
        on_event: Optional[EventSink] = None,
        max_steps: int = 6,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.conversation = conversation
        self.approval = approval
        self.on_event = on_event
        self.max_steps = max_steps

    def _emit(self, event: AgentEvent) -> None:
        if self.on_event:
            self.on_event(event)

    async def send(self, user_text: str) -> str:
        """Process one user message and return the assistant's final answer."""
        self.conversation.add_user(user_text)
        tools = self.registry.tool_specs()

        for step in range(1, self.max_steps + 1):
            self._emit(AgentEvent(kind="thinking", data={"step": step}))
            try:
                turn: LLMTurn = await self.llm.generate(
                    system_instruction=self.conversation.system_instruction,
                    messages=self.conversation.messages,
                    tools=tools,
                )
            except LLMError as exc:
                message = f"No pude consultar al modelo: {exc}"
                self._emit(AgentEvent(kind="error", text=message))
                self.conversation.add_assistant(message)
                return message

            if not turn.wants_tools:
                answer = turn.text or "No obtuve una respuesta del modelo. Intente de nuevo."
                self.conversation.add_assistant(answer)
                self._emit(AgentEvent(kind="answer", text=answer, data=turn.usage or {}))
                return answer

            # The model asked for tools: record the request, run them, loop.
            self.conversation.add_assistant(text=turn.text, tool_calls=turn.tool_calls)
            results = [await self._run_tool(call) for call in turn.tool_calls]
            self.conversation.add_tool_results(results)

        exhausted = (
            "La consulta requirio demasiados pasos y se detuvo por seguridad. "
            "Intente formularla de manera mas concreta."
        )
        self.conversation.add_assistant(exhausted)
        self._emit(AgentEvent(kind="error", text=exhausted))
        return exhausted

    async def _run_tool(self, call: ToolCall) -> ToolResult:
        registered = self.registry.find(call.name)
        self._emit(AgentEvent(kind="tool_call", tool_call=call))

        if registered is not None and registered.requires_approval:
            approved = await self._ask_approval(call, registered)
            if not approved:
                denied = ToolResult(
                    call=call,
                    text=(
                        "El usuario no autorizo esta operacion. No se realizo ningun "
                        "cambio. Preguntale que desea corregir antes de reintentar."
                    ),
                    is_error=True,
                )
                self._emit(AgentEvent(kind="tool_denied", tool_call=call, result=denied))
                return denied

        outcome = await self.registry.call(call.name, call.arguments)
        result = ToolResult(
            call=call,
            text=outcome.as_text(),
            is_error=outcome.isError,
            data=outcome.structuredContent,
        )
        self._emit(AgentEvent(kind="tool_result", tool_call=call, result=result))
        return result

    async def _ask_approval(self, call: ToolCall, registered: RegisteredTool) -> bool:
        """Human in the loop for every tool that is not read-only."""
        if self.approval is None:
            logger.warning(
                "Tool '%s' writes but no approval callback is configured; allowing it",
                call.name,
            )
            return True
        return await self.approval(call, registered)

    def pending_write_tools(self) -> List[str]:
        """Tools that will trigger a confirmation prompt, for the UI to show."""
        return [tool.qualified_name for tool in self.registry.tools if tool.requires_approval]
