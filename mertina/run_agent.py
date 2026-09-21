# Ported from hermes-agent run_agent.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""AIAgent: the tool-calling agent runner (conversation loop, tool execution, session lifecycle).

from mertina.run_agent import AIAgent
agent = AIAgent(base_url="http://localhost:30000/v1", model="claude-opus-4-20250514")
response = agent.run_conversation("Tell me about the latest Python updates")
"""

# hermes_bootstrap must be the very first import (UTF-8 stdio on Windows; no-op on POSIX).
try:
    from mertina import bootstrap  # noqa: F401
except ModuleNotFoundError:
    pass  # partial `hermes update` — only skips the Windows UTF-8 stdio setup

import logging

logger = logging.getLogger(__name__)
