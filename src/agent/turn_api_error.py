"""API-call exception handling for the v0.1 conversation retry loop."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Dict, Optional

from agent.retry_utils import jittered_backoff, parse_retry_after_seconds


@dataclass
class ApiErrorVerdict:
    action: str
    thinking_spinner: Any
    messages: Any
    active_system_prompt: Any
    conversation_history: Any
    approx_tokens: Any
    retry_count: Any
    max_retries: Any
    compression_attempts: Any
    _provider_overflow_recovery_pending: Any
    result: Optional[Dict[str, Any]] = None


def handle_api_error(
    agent: Any, *, api_error: Any, _retry: Any, thinking_spinner: Any, messages: Any,
    api_messages: Any, api_kwargs: Any, system_message: Any, active_system_prompt: Any,
    conversation_history: Any, approx_tokens: Any, retry_count: Any, max_retries: Any,
    compression_attempts: Any, max_compression_attempts: Any, api_call_count: Any,
    api_request_id: Any, api_start_time: Any, effective_task_id: Any, turn_id: Any,
) -> ApiErrorVerdict:
    def _verdict(action: str, result: Optional[Dict[str, Any]] = None) -> ApiErrorVerdict:
        return ApiErrorVerdict(
            action=action, thinking_spinner=thinking_spinner, messages=messages,
            active_system_prompt=active_system_prompt, conversation_history=conversation_history,
            approx_tokens=approx_tokens, retry_count=retry_count, max_retries=max_retries,
            compression_attempts=compression_attempts,
            _provider_overflow_recovery_pending=False, result=result,
        )

    if thinking_spinner:
        thinking_spinner.stop("")
        thinking_spinner = None
    if agent.thinking_callback:
        agent.thinking_callback("")

    status_code = getattr(api_error, "status_code", None)
    retry_count += 1
    retryable = status_code == 429 or isinstance(status_code, int) and status_code >= 500
    if retryable and retry_count < max_retries and not agent._interrupt_requested:
        headers = getattr(getattr(api_error, "response", None), "headers", None)
        wait_time = parse_retry_after_seconds(headers)
        if wait_time is None:
            wait_time = jittered_backoff(retry_count, base_delay=1.0, max_delay=30.0)
        time.sleep(wait_time)
        return _verdict("continue")

    error_msg = str(api_error)
    return _verdict("return", {
        "final_response": error_msg, "messages": messages, "api_calls": api_call_count,
        "completed": False, "failed": True, "error": error_msg,
        "failure_reason": "rate_limit" if status_code == 429 else "server_error" if retryable else "api_error",
        "failure_retryable": retryable,
    })
