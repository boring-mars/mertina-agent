# Ported from hermes-agent run_agent.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
#!/usr/bin/env python3
"""AIAgent: the tool-calling agent runner (conversation loop, tool execution).

from run_agent import AIAgent
agent = AIAgent(base_url="http://localhost:30000/v1", model="claude-opus-4-20250514")
response = agent.run_conversation("Tell me about the latest Python updates")
"""

import logging

logger = logging.getLogger(__name__)
import sys  # noqa: E402  # upstream order
from typing import Any  # noqa: E402  # upstream order

from mertina.agent.client_lifecycle import ClientLifecycleMixin  # noqa: E402  # upstream order
from mertina.agent.interrupt_control import InterruptControlMixin  # noqa: E402  # upstream order
from mertina.agent.lazy_forward import forward as _forward  # noqa: E402  # upstream order
from mertina.agent.reasoning_params import ReasoningParamsMixin  # noqa: E402  # upstream order
from mertina.agent.status_output import StatusOutputMixin  # noqa: E402  # upstream order
from mertina.agent.turn_facade import TurnFacadeMixin  # noqa: E402  # upstream order
from mertina.agent.vision_message_prep import VisionMessagePrepMixin  # noqa: E402  # upstream order


class AIAgent(
    ClientLifecycleMixin,
    StatusOutputMixin,
    InterruptControlMixin,
    TurnFacadeMixin,
    VisionMessagePrepMixin,
    ReasoningParamsMixin,
):
    """AI Agent with tool calling capabilities."""

    @property
    def base_url(self) -> str:
        return self._base_url

    @base_url.setter
    def base_url(self, value: str) -> None:
        self._base_url = value

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        provider: str | None = None,
        model: str = "",
        max_iterations: int = sys.maxsize,  # unlimited tool-calling iterations by default
        verbose_logging: bool = False,
        quiet_mode: bool = False,
        log_prefix: str = "",
        session_id: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        """Forwarder — see ``agent.agent_init.init_agent`` (same keyword parameters)."""
        init_kwargs = {k: v for k, v in locals().items() if k not in ("self",)}
        from mertina.agent.agent_init import init_agent

        init_agent(self, **init_kwargs)

    def _max_tokens_param(self, value: int) -> dict[str, int]:
        """``max_tokens`` for the request."""
        return {"max_tokens": value}

    _strip_think_blocks = _forward("mertina.agent.agent_runtime_helpers", "strip_think_blocks")

    _extract_reasoning = _forward("mertina.agent.agent_runtime_helpers", "extract_reasoning")

    _build_system_prompt = _forward("mertina.agent.system_prompt", "build_system_prompt")

    @staticmethod
    def _get_tool_call_name_static(tc: Any) -> str:
        """Function name of a tool_call entry (dict or object); Gemini requires it on every ``role:
        tool`` message."""
        if isinstance(tc, dict):
            fn = tc.get("function")
            return (fn.get("name", "") or "") if isinstance(fn, dict) else ""
        return getattr(getattr(tc, "function", None), "name", "") or ""

    _interruptible_api_call = _forward(
        "mertina.agent.chat_completion_helpers", "interruptible_api_call"
    )
    _build_api_kwargs = _forward("mertina.agent.chat_completion_helpers", "build_api_kwargs")

    def _execute_tool_calls(
        self,
        assistant_message: Any,
        messages: list[dict[str, Any]],
        effective_task_id: str,
        api_call_count: int = 0,
    ) -> None:
        """Execute the assistant's tool calls and append results to ``messages``.

        A single call runs sequentially; a batch of several runs concurrently.
        """
        tool_calls = assistant_message.tool_calls
        args = (assistant_message, messages, effective_task_id, api_call_count)
        if len(tool_calls) <= 1:
            self._execute_tool_calls_sequential(*args)
        else:
            run = self._execute_tool_calls_concurrent
            run(*args)

    _invoke_tool = _forward("mertina.agent.agent_runtime_helpers", "invoke_tool")

    _execute_tool_calls_concurrent = _forward(
        "mertina.agent.tool_executor", "execute_tool_calls_concurrent"
    )
    _execute_tool_calls_sequential = _forward(
        "mertina.agent.tool_executor", "execute_tool_calls_sequential"
    )
    _handle_max_iterations = _forward(
        "mertina.agent.chat_completion_helpers", "handle_max_iterations"
    )


def main(
    query: str | None = None,
    model: str = "",
    api_key: str | None = None,
    base_url: str = "",
    max_turns: int = 10,
    verbose: bool = False,
) -> None:
    """
    Main function for running the agent directly.

    Args:
        query (str): Natural language query for the agent. Defaults to Python 3.13 example.
        model (str): Model name to use.
        api_key (str): API key for authentication.
        base_url (str): Base URL for the model API.
        max_turns (int): Maximum number of API call iterations. Defaults to 10.
        verbose (bool): Enable verbose logging for debugging. Defaults to False.
    """
    print("🤖 AI Agent with Tool Calling")
    print("=" * 50)

    try:
        agent = AIAgent(
            base_url=base_url,
            model=model,
            api_key=api_key,
            max_iterations=max_turns,
            verbose_logging=verbose,
        )
    except RuntimeError as e:
        print(f"❌ Failed to initialize agent: {e}")
        return

    user_query = (
        query
        if query is not None
        else (
            "Tell me about the latest developments in Python 3.13 and what new features "
            "developers should know about. Please search for current information and try it out."
        )
    )
    print(f"\n📝 User Query: {user_query}")
    print("\n" + "=" * 50)

    result = agent.run_conversation(user_query)

    print("\n" + "=" * 50 + "\n📋 CONVERSATION SUMMARY\n" + "=" * 50)
    print(
        f"✅ Completed: {result['completed']}\n📞 API Calls: {result['api_calls']}\n💬 Messages: {len(result['messages'])}"  # noqa: E501  # upstream's message
    )
    if result["final_response"]:
        print("\n🎯 FINAL RESPONSE:\n" + "-" * 30 + "\n" + result["final_response"])
    print("\n👋 Agent execution completed!")


if __name__ == "__main__":
    import fire  # type: ignore[import-untyped]  # fire ships no type information

    fire.Fire(main)
