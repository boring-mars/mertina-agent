# Ported from hermes-agent run_agent.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""AIAgent: the tool-calling agent runner (conversation loop, tool execution, session lifecycle).

from mertina.run_agent import AIAgent
agent = AIAgent(base_url="http://localhost:30000/v1", model="claude-opus-4-20250514")
response = agent.run_conversation("Tell me about the latest Python updates")
"""

import logging

logger = logging.getLogger(__name__)
