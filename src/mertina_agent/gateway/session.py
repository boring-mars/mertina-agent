"""Construct independently owned agents with explicit production tool registration."""

from collections.abc import Sequence
from typing import Protocol

from mertina_agent.agent.core import Agent
from mertina_agent.agent.events import EventCallback
from mertina_agent.agent.transports.types import ChatMessage
from mertina_agent.agent.turn_result import ConversationResult
from mertina_agent.config import Settings
from mertina_agent.tools.registry import ToolRegistry
from mertina_agent.tools.web_tools import build_search_provider, register_web_search


class SessionAgent(Protocol):
    """One run's agent and its owned model resources."""

    async def run_conversation(
        self, user_message: str, *, conversation_history: Sequence[ChatMessage] | None = None
    ) -> ConversationResult:
        """Return the complete, closed history of this turn."""
        ...

    def interrupt(self, reason: str | None = None) -> bool:
        """Request cooperative interruption of an active turn."""
        ...

    async def aclose(self) -> None:
        """Release the model resources owned by this agent."""
        ...


class AgentFactory(Protocol):
    """Create one owned agent; test factories own any injected model clients."""

    def __call__(self, callback: EventCallback) -> SessionAgent:
        """Construct an agent using the current process configuration."""
        ...


def build_agent_factory(settings: Settings) -> AgentFactory:
    """Register configured tools once and create a fresh model client per run."""
    registry = ToolRegistry()
    register_web_search(build_search_provider(settings), tool_registry=registry)

    def create(callback: EventCallback) -> SessionAgent:
        return Agent(settings, tool_registry=registry, event_callback=callback)

    return create
