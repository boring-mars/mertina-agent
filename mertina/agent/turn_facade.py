# Ported from hermes-agent agent/turn_facade.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""``AIAgent.run_conversation`` / ``chat`` façade around ``conversation_loop.run_conversation``.
Extracted from ``run_agent.py``; every method resolves through ``AIAgent``'s MRO unchanged.
"""

import uuid
from typing import Any


class TurnFacadeMixin:
    """run_conversation()/chat() (see module docstring)."""

    def run_conversation(
        self,
        user_message: Any,
        system_message: str = None,
        conversation_history: list[dict[str, Any]] = None,
        task_id: str = None,
    ) -> dict[str, Any]:
        """Forwarder — see ``agent.conversation_loop.run_conversation``."""
        from mertina.agent.conversation_loop import run_conversation

        effective_task_id = task_id or str(uuid.uuid4())

        result = run_conversation(
            self,
            user_message,
            system_message,
            conversation_history,
            effective_task_id,
        )
        return result

    def chat(self, message: str) -> str:
        """Final response string of one turn."""
        return self.run_conversation(message)["final_response"]
