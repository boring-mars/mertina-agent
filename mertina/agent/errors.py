# Ported from hermes-agent agent/errors.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
class EmptyStreamError(RuntimeError):
    """Raised when a provider closes a stream without yielding a response."""
