"""``AIAgent.run_conversation`` / ``chat`` façade.

Turn admission around ``conversation_loop.run_conversation``: durable cross-process session turn lease +
refresher thread and liveness watchdog (``agent.turn_facade_lease``), relay/accounting/portal scopes, and
balanced start/finish marks. Extracted from ``run_agent.py``; every method resolves through ``AIAgent``'s
MRO unchanged.
"""
import logging
import uuid
from contextlib import suppress
from typing import Any, Dict, List, Optional

from agent.lazy_forward import forward as _forward

# Same logger name as the origin module so log records / caplog filters are unchanged.
logger = logging.getLogger("run_agent")


class TurnFacadeMixin:
    """run_conversation()/chat() (see module docstring)."""

    def run_conversation(
        self, user_message: Any, system_message: str=None,
        conversation_history: List[Dict[str, Any]]=None, task_id: str=None,
        stream_callback: Optional[callable]=None, persist_user_message: Optional[Any]=None,
        persist_user_timestamp: Optional[float]=None, persist_user_display_kind: Optional[str]=None,
        persist_user_display_metadata: Optional[Dict[str, Any]]=None,
        persist_user_platform_id: Optional[str]=None, moa_config: Optional[dict[str, Any]]=None,
        turn_author: Optional[Dict[str, Any]] = None,
        relay_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Forwarder — see ``agent.conversation_loop.run_conversation``."""
        from agent.conversation_loop import run_conversation

        effective_task_id = task_id or str(uuid.uuid4())
        return run_conversation(
            self, user_message, system_message, conversation_history, effective_task_id,
            stream_callback, persist_user_message,
            persist_user_timestamp=persist_user_timestamp,
            persist_user_display_kind=persist_user_display_kind,
            persist_user_display_metadata=persist_user_display_metadata,
            persist_user_platform_id=persist_user_platform_id, moa_config=moa_config,
            turn_author=turn_author,
        )

    def chat(self, message: str, stream_callback: Optional[callable] = None) -> str:
        """Final response string of one turn; ``stream_callback`` receives each text delta."""
        return self.run_conversation(message, stream_callback=stream_callback)["final_response"]

    _run_codex_app_server_turn = _forward("agent.codex_runtime", "run_codex_app_server_turn")
