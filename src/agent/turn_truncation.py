"""Minimal truncation and refusal handling for the v0.1 Chat Completions loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


def normalize_response_for_agent(agent: Any, response: Any) -> Any:
    """Normalize a raw response through the active transport."""
    return agent._get_transport().normalize_response(response)


def partial_result(
    messages: List[Dict[str, Any]], api_call_count: int, final_response: str,
    error: Optional[str] = None, *, failed: bool = False,
) -> Dict[str, Any]:
    return {
        "final_response": final_response,
        "messages": messages,
        "api_calls": api_call_count,
        "completed": False,
        ("failed" if failed else "partial"): True,
        "error": final_response if error is None else error,
    }


@dataclass
class TruncationVerdict:
    action: str
    result: Optional[Dict[str, Any]]
    messages: List[Dict[str, Any]]
    length_continue_retries: int
    truncated_response_parts: List[str]
    truncated_tool_call_retries: int
    retry_count: int
    compression_attempts: int


def recover_from_truncation(
    agent: Any, response: Any, finish_reason: str, _retry: Any, *,
    messages: List[Dict[str, Any]], conversation_history: Any, api_kwargs: Any,
    api_call_count: int, effective_task_id: Any, current_turn_user_idx: Any,
    length_continue_retries: int, truncated_response_parts: List[str],
    truncated_tool_call_retries: int, retry_count: int, compression_attempts: int,
) -> TruncationVerdict:
    del finish_reason, _retry, conversation_history, api_kwargs
    del effective_task_id, current_turn_user_idx
    normalized = normalize_response_for_agent(agent, response)
    content = (getattr(normalized, "content", None) or "").strip()
    result = partial_result(
        messages, api_call_count, content,
        error="model response ended because the output limit was reached",
    )
    return TruncationVerdict(
        "return", result, messages, length_continue_retries,
        truncated_response_parts, truncated_tool_call_retries,
        retry_count, compression_attempts,
    )


@dataclass
class RefusalVerdict:
    action: str
    result: Optional[Dict[str, Any]]
    active_system_prompt: Any


def handle_content_policy_refusal(
    agent: Any, response: Any, _retry: Any, *, thinking_spinner: Any,
    messages: List[Dict[str, Any]], api_messages: Any, api_kwargs: Any,
    active_system_prompt: Any, conversation_history: Any, api_call_count: int,
    effective_task_id: Any, turn_id: Any, api_request_id: Any,
    api_start_time: float, retry_count: int, max_retries: int,
) -> RefusalVerdict:
    del _retry, api_messages, conversation_history
    normalized = normalize_response_for_agent(agent, response)
    text = (getattr(normalized, "content", None) or "").strip()
    if thinking_spinner:
        thinking_spinner.stop("")
    agent._invoke_api_request_error_hook(
        task_id=effective_task_id, turn_id=turn_id,
        api_request_id=api_request_id, api_call_count=api_call_count,
        api_start_time=api_start_time, api_kwargs=api_kwargs,
        error_type="ContentPolicyBlocked",
        error_message=text or "model declined to respond",
        status_code=None, retry_count=retry_count,
        max_retries=max_retries, retryable=False,
        reason="content_policy_blocked",
    )
    from agent.conversation_loop import _content_policy_blocked_result
    result = _content_policy_blocked_result(
        messages, api_call_count,
        final_response=text or "The model declined to respond to this request.",
        error_detail=text or "model declined to respond",
    )
    return RefusalVerdict("return", result, active_system_prompt)
