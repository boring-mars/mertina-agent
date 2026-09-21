"""The public agent: one object that holds configuration and runs conversation turns.

Adapted from Hermes run_agent.py (``AIAgent``) and agent/turn_facade.py
(``TurnFacadeMixin.run_conversation`` / ``chat``) at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-agent-core.md.

Hermes composes ``AIAgent`` from fourteen mixins and more than a hundred
constructor parameters. Mertina keeps what the loop needs as a plain class: the
tool selection resolved once at construction, the system prompt built once on
the first turn and replayed verbatim afterwards, and one turn at a time.
"""

import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from types import TracebackType
from typing import Self, cast

from mertina_agent.agent.conversation_loop import run_conversation
from mertina_agent.agent.events import EventCallback, RunCompleted, emit_event
from mertina_agent.agent.interrupt import InterruptSignal, bind_interrupt_signal
from mertina_agent.agent.model_client import ModelClient, ModelClientProtocol
from mertina_agent.agent.system_prompt import build_system_prompt
from mertina_agent.agent.transports.types import ChatMessage, ToolDefinition
from mertina_agent.agent.turn_result import ConversationResult
from mertina_agent.config import Settings
from mertina_agent.exceptions import AgentBusyError, ModelInputError
from mertina_agent.tools.model_tools import get_tool_definitions
from mertina_agent.tools.registry import ToolRegistry, registry

logger = logging.getLogger(__name__)


def _local_now() -> datetime:
    return datetime.now().astimezone()


class Agent:
    """A tool-using agent that runs ReAct-style turns against one model endpoint.

    The caller owns the conversation: each turn takes the previous history and
    returns the new one, which can be passed straight back into the next turn.

    Attributes:
        tools: Tool declarations offered to the model, fixed at construction.
        valid_tool_names: Names of the offered tools.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        model_client: ModelClientProtocol | None = None,
        tool_registry: ToolRegistry = registry,
        enabled_toolsets: Sequence[str] | None = None,
        disabled_toolsets: Sequence[str] | None = None,
        system_message: str | None = None,
        event_callback: EventCallback | None = None,
        clock: Callable[[], datetime] = _local_now,
    ) -> None:
        """Create an agent.

        Args:
            settings: Validated configuration; supplies the model and iteration budget.
            model_client: Model-call boundary. When omitted, the agent creates a
                :class:`ModelClient` from ``settings`` and closes it in :meth:`aclose`.
            tool_registry: Registry that owns the tools.
            enabled_toolsets: Toolsets to offer; ``None`` offers every registered one.
            disabled_toolsets: Toolsets removed after enabling.
            system_message: Extra instructions added to the system prompt.
            event_callback: Receives progress events; failures in it are logged only.
            clock: Returns the current timezone-aware time for the prompt's date line.

        Raises:
            ConfigurationError: If a named toolset has no registered tools.
        """
        self._settings = settings
        self._owns_model_client = model_client is None
        self._model_client: ModelClientProtocol = (
            model_client if model_client is not None else ModelClient(settings)
        )
        self._tool_registry = tool_registry
        self._system_message = system_message
        self._event_callback = event_callback
        self._clock = clock
        self.tools: list[ToolDefinition] = get_tool_definitions(
            enabled_toolsets, disabled_toolsets, tool_registry=tool_registry
        )
        self.valid_tool_names: frozenset[str] = frozenset(
            tool["function"]["name"] for tool in self.tools
        )
        self._cached_system_prompt: str | None = None
        self._interrupt = InterruptSignal()
        self._running = False
        self._closed = False

    @property
    def max_iterations(self) -> int:
        """Model calls a single turn may spend on the tool loop."""
        return self._settings.max_iterations

    def _system_prompt(self) -> str:
        # Built once and replayed verbatim so the request prefix stays stable.
        if self._cached_system_prompt is None:
            self._cached_system_prompt = build_system_prompt(
                model=self._settings.llm_model,
                valid_tool_names=self.valid_tool_names,
                now=self._clock(),
                system_message=self._system_message,
            )
        return self._cached_system_prompt

    async def run_conversation(
        self,
        user_message: str,
        *,
        conversation_history: Sequence[ChatMessage] | None = None,
    ) -> ConversationResult:
        """Run one user turn to completion.

        Args:
            user_message: The user's text.
            conversation_history: Earlier messages, typically the ``messages``
                of the previous result. Never modified.

        Returns:
            The turn result; its ``messages`` include this turn.

        Raises:
            AgentBusyError: If a turn is already running on this agent.
            ModelInputError: If the message or history violates the text contract.
            ModelRequestError: If the agent's client has been closed.
        """
        if self._running:
            message = "Agent is already running a turn"
            raise AgentBusyError(message)
        # Widen at the runtime boundary: callers are not obliged to use mypy.
        if not isinstance(cast(object, user_message), str):
            message = "user_message must be text"
            raise ModelInputError(message)

        self._running = True
        try:
            with bind_interrupt_signal(self._interrupt):
                result = await run_conversation(
                    user_message,
                    list(conversation_history) if conversation_history is not None else None,
                    model_client=self._model_client,
                    active_system_prompt=self._system_prompt(),
                    tools=self.tools,
                    valid_tool_names=self.valid_tool_names,
                    tool_registry=self._tool_registry,
                    max_iterations=self.max_iterations,
                    event_callback=self._event_callback,
                    turn_id=uuid.uuid4().hex,
                )
        finally:
            self._running = False
            # A stop request applies to one turn; it must not leak into the next.
            self._interrupt.clear()

        emit_event(
            self._event_callback,
            RunCompleted(
                final_response=result["final_response"],
                completed=result["completed"],
                turn_exit_reason=result["turn_exit_reason"],
            ),
        )
        return result

    async def chat(self, message: str) -> str:
        """Run one turn without history and return only the final text.

        Returns ``""`` when the turn produced no text; use
        :meth:`run_conversation` to see why.
        """
        result = await self.run_conversation(message)
        return result["final_response"] or ""

    async def aclose(self) -> None:
        """Close the model client if this agent created it."""
        if self._closed:
            return
        self._closed = True
        if self._owns_model_client:
            await self._model_client.aclose()

    async def __aenter__(self) -> Self:
        """Enter an agent context; the owned client is closed on exit."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release owned resources on success, failure or cancellation."""
        await self.aclose()
