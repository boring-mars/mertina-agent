# Ported from hermes-agent agent/context_compressor.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""The iteration-summary request text (upstream's home for automatic context compression, which
v0.1 does not port)."""

# Content string is the authoritative marker: SessionDB drops ``_``-metadata.
MAX_ITERATIONS_SUMMARY_REQUEST = (
    "You've reached the maximum number of tool-calling iterations allowed. Please provide a final response "
    "summarizing what you've found and accomplished so far, without calling any more tools."
)
