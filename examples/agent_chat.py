"""Run one agent turn against an explicitly configured endpoint, with web search.

The model is offered the built-in ``web_search`` tool, configured from the same
settings, and a ``get_current_time`` tool that exists only in this example. Like
``model_call.py``, it is never part of the default test run and requires an
existing env file with an explicit endpoint and model, so an accidental run
cannot fall back to a hosted default. Web searches go to DuckDuckGo.
"""

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from mertina_agent.agent.core import Agent
from mertina_agent.agent.events import (
    AgentEvent,
    RetryScheduled,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from mertina_agent.agent.model_client import ModelClient
from mertina_agent.agent.transports.types import FunctionDefinition, JsonObject
from mertina_agent.config import Settings, load_settings
from mertina_agent.exceptions import ConfigurationError, MertinaError
from mertina_agent.tools.registry import ToolRegistry, tool_result
from mertina_agent.tools.web_tools import build_search_provider, register_web_search

DEFAULT_PROMPT = (
    "Search the web for the latest stable Python release and answer in one sentence, "
    "naming the version and citing one source URL."
)

CURRENT_TIME_SCHEMA: FunctionDefinition = {
    "name": "get_current_time",
    "description": "Return the current local date and time of the machine running the agent.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


def _get_current_time(_args: JsonObject) -> str:
    return tool_result(now=datetime.now().astimezone().isoformat(timespec="seconds"))


def build_demo_registry(settings: Settings) -> ToolRegistry:
    """Return a registry with ``web_search`` from ``settings`` and the demonstration clock."""
    demo_registry = ToolRegistry()
    register_web_search(build_search_provider(settings), tool_registry=demo_registry)
    demo_registry.register("get_current_time", "demo", CURRENT_TIME_SCHEMA, _get_current_time)
    return demo_registry


def _explicit_settings(env_file: Path) -> Settings:
    if not env_file.is_file():
        message = "The example requires an existing env file."
        raise ConfigurationError(message)
    settings = load_settings(env_file=env_file)
    if not {"llm_base_url", "llm_model"}.issubset(settings.model_fields_set):
        message = "Explicit MERTINA_LLM_BASE_URL and MERTINA_LLM_MODEL are required."
        raise ConfigurationError(message)
    return settings


def _printable(text: str) -> str:
    """Escape control characters so model output cannot drive the terminal."""
    return "".join(
        char if char in "\n\t" or char.isprintable() else repr(char)[1:-1] for char in text
    )


def _report_event(event: AgentEvent) -> None:
    # Progress goes to stderr so stdout stays a single JSON document.
    if isinstance(event, TextDelta):
        sys.stderr.write(_printable(event.text))
    elif isinstance(event, RetryScheduled):
        sys.stderr.write(f"\n[retrying in {event.wait_s:.1f}s: {event.reason}]\n")
    elif isinstance(event, ToolCallStarted):
        sys.stderr.write(f"-> tool {event.name} ({event.call_id})\n")
    elif isinstance(event, ToolCallFinished):
        outcome = "error" if event.is_error else "ok"
        sys.stderr.write(f"<- tool {event.name} {outcome} in {event.duration_s:.2f}s\n")


async def _run_turn(settings: Settings, prompt: str) -> bool:
    async with (
        ModelClient(settings) as client,
        Agent(
            settings,
            model_client=client,
            tool_registry=build_demo_registry(settings),
            event_callback=_report_event,
        ) as agent,
    ):
        result = await agent.run_conversation(prompt)
    sys.stderr.write("\n")  # End the streamed text before the summary.
    tools_used = [
        call["function"]["name"]
        for message in result["messages"]
        if message["role"] == "assistant"
        for call in message.get("tool_calls") or ()
    ]
    usage = result["usage"]
    summary = {
        "final_response": result["final_response"],
        "completed": result["completed"],
        "turn_exit_reason": result["turn_exit_reason"],
        "api_calls": result["api_calls"],
        "tools_used": tools_used,
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
        },
    }
    # JSON escapes terminal control characters in model output. No configuration,
    # full history or exception chain is included in this presentation.
    sys.stdout.write(json.dumps(summary, ensure_ascii=True, indent=2) + "\n")
    return not result["failed"]


def main(argv: Sequence[str] | None = None) -> int:
    """Run one explicit agent turn and return a process exit status.

    Args:
        argv: Command-line arguments, or ``None`` for the process arguments.

    Returns:
        Zero when the turn ran, one when it failed (the summary still explains
        why), two for invalid configuration, or 130 when the caller interrupts
        the process.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT, help="user message")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="existing dotenv file (default: .env); environment variables take precedence",
    )
    args = parser.parse_args(argv)
    try:
        settings = _explicit_settings(args.env_file)
    except (ConfigurationError, OSError, UnicodeError):
        sys.stderr.write(
            "Configuration error: provide an existing env file and valid explicit "
            "MERTINA_LLM_BASE_URL / MERTINA_LLM_MODEL settings. "
            "A key is needed only when your endpoint requires one.\n"
        )
        return 2

    # SDK debug environment variables must not expose request payloads here.
    for logger_name in ("openai", "httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    try:
        succeeded = asyncio.run(_run_turn(settings, args.prompt))
    except MertinaError:
        # Chained SDK exceptions may contain echoed prompts or credentials.
        sys.stderr.write("Agent turn failed: invalid input, configuration, or response.\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("Agent turn interrupted.\n")
        return 130
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
