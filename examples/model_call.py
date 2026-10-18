"""Explicitly smoke-test one configured endpoint without executing returned tools.

This script is never part of the default test run. It deliberately requires an
existing env file and explicitly supplied endpoint/model fields so an accidental
invocation cannot select a hosted service through library defaults alone.
"""

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from mertina_agent.agent.model_client import ModelClient
from mertina_agent.agent.transports.types import ChatMessage, ToolDefinition
from mertina_agent.config import Settings, load_settings
from mertina_agent.exceptions import ConfigurationError, MertinaError, ModelRequestError

DEFAULT_PROMPT = "Reply with a short greeting."
TOOL_PROMPT = "Request the echo tool with text 'hello'. Do not execute it."


def _explicit_settings(env_file: Path) -> Settings:
    if not env_file.is_file():
        message = "The example requires an existing env file."
        raise ConfigurationError(message)
    settings = load_settings(env_file=env_file)
    # BaseSettings tracks fields supplied by dotenv/environment separately from
    # defaults. This avoids duplicating dotenv parsing or changing library defaults.
    if not {"llm_base_url", "llm_model"}.issubset(settings.model_fields_set):
        message = "Explicit MERTINA_LLM_BASE_URL and MERTINA_LLM_MODEL are required."
        raise ConfigurationError(message)
    return settings


async def _complete(settings: Settings, *, with_tools: bool) -> None:
    messages: list[ChatMessage] = [
        {"role": "user", "content": TOOL_PROMPT if with_tools else DEFAULT_PROMPT}
    ]
    tools: list[ToolDefinition] = []
    if with_tools:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "Echo the supplied text; this example never executes the tool.",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                },
            }
        )
    async with ModelClient(settings) as client:
        result = await client.complete(messages, tools=tools)
    # JSON escapes terminal control characters in model output. No configuration,
    # prompt, raw SDK body, or exception chain is included in this presentation.
    sys.stdout.write(json.dumps(asdict(result), ensure_ascii=True, indent=2) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Run a single explicit smoke request and return a process exit status.

    Args:
        argv: Command-line arguments, or ``None`` for the process arguments.

    Returns:
        Zero for a valid response, one for a model-layer failure, two for invalid
        configuration, or 130 when the caller interrupts the process.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="existing dotenv file (default: .env); environment variables take precedence",
    )
    parser.add_argument(
        "--tools",
        action="store_true",
        help="offer a demonstration echo tool and display, but never execute, any tool request",
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

    # The SDK can enable debug output through its own environment variables.
    # A smoke example must not expose request payloads through those diagnostics.
    for logger_name in ("openai", "httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    try:
        asyncio.run(_complete(settings, with_tools=args.tools))
    except ModelRequestError as exc:
        status = f", HTTP {exc.status_code}" if exc.status_code is not None else ""
        sys.stderr.write(f"Model request failed ({exc.kind}{status}).\n")
        return 1
    except MertinaError:
        # Chained SDK exceptions may contain echoed prompts or credentials.
        # Keep diagnostic text controlled instead of displaying str(exc).
        sys.stderr.write("Model call failed: invalid input, configuration, or response.\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("Model call interrupted.\n")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
