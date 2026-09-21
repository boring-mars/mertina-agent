"""Stop a turn while a tool runs, then continue the conversation from its history.

The first turn is stopped as soon as the model starts a tool call. The second
turn sends the stopped turn's history back unchanged; a provider that rejects
malformed histories (unanswered tool calls, ``tool -> user`` tails) would fail
here. Like the other examples, this script needs an existing env file with an
explicit endpoint and model, and is never part of the default test run.
"""

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from mertina_agent.agent.core import Agent
from mertina_agent.agent.events import AgentEvent, ToolCallStarted
from mertina_agent.agent.model_client import ModelClient
from mertina_agent.agent.turn_result import ConversationResult
from mertina_agent.config import Settings, load_settings
from mertina_agent.exceptions import ConfigurationError, MertinaError
from mertina_agent.tools.registry import ToolRegistry
from mertina_agent.tools.web_tools import build_search_provider, register_web_search

FIRST_PROMPT = (
    "Search the web for the current stable versions of Python and of Node.js, then compare them."
)
RESUME_PROMPT = "You were stopped. Continue from what you have and answer in two sentences."


def _explicit_settings(env_file: Path) -> Settings:
    if not env_file.is_file():
        message = "The example requires an existing env file."
        raise ConfigurationError(message)
    settings = load_settings(env_file=env_file)
    if not {"llm_base_url", "llm_model"}.issubset(settings.model_fields_set):
        message = "Explicit MERTINA_LLM_BASE_URL and MERTINA_LLM_MODEL are required."
        raise ConfigurationError(message)
    return settings


def _summary(result: ConversationResult) -> dict[str, object]:
    return {
        "final_response": result["final_response"],
        "completed": result["completed"],
        "interrupted": result["interrupted"],
        "turn_exit_reason": result["turn_exit_reason"],
        "api_calls": result["api_calls"],
        "history_roles": [message["role"] for message in result["messages"]],
    }


async def _stop_and_resume(settings: Settings) -> bool:
    tool_registry = ToolRegistry()
    register_web_search(build_search_provider(settings), tool_registry=tool_registry)
    holder: dict[str, Agent] = {}

    def stop_on_first_tool(event: AgentEvent) -> None:
        if isinstance(event, ToolCallStarted) and holder["agent"].interrupt("example_stop"):
            sys.stderr.write(f"stop requested while {event.name} runs\n")

    async with (
        ModelClient(settings) as client,
        Agent(
            settings,
            model_client=client,
            tool_registry=tool_registry,
            event_callback=stop_on_first_tool,
        ) as agent,
    ):
        holder["agent"] = agent
        stopped = await agent.run_conversation(FIRST_PROMPT)
        resumed = await agent.run_conversation(
            RESUME_PROMPT, conversation_history=stopped["messages"]
        )
    report = {"stopped_turn": _summary(stopped), "resumed_turn": _summary(resumed)}
    # JSON escapes terminal control characters in model output.
    sys.stdout.write(json.dumps(report, ensure_ascii=True, indent=2) + "\n")
    return stopped["interrupted"] and resumed["completed"]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the stop-and-resume scenario and return a process exit status.

    Returns:
        Zero when the first turn was stopped and the second completed, one
        otherwise, two for invalid configuration, or 130 on keyboard interrupt.
    """
    parser = argparse.ArgumentParser(description=__doc__)
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
            "MERTINA_LLM_BASE_URL / MERTINA_LLM_MODEL settings.\n"
        )
        return 2

    for logger_name in ("openai", "httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    try:
        succeeded = asyncio.run(_stop_and_resume(settings))
    except MertinaError:
        sys.stderr.write("Scenario failed: invalid input, configuration, or response.\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("Scenario interrupted.\n")
        return 130
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
