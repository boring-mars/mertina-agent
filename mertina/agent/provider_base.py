# Ported from hermes-agent agent/provider_base.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Shared base classes for the pluggable-backend provider ABCs.

Every tool-provider ABC shares the same identity surface. Concrete ABCs subclass
:class:`ProviderBase` and add only their domain methods. Plugins keep subclassing the
concrete ABC, so ``isinstance`` checks and abstract-method sets are unchanged.
"""

from __future__ import annotations

import abc


class ProviderBase(abc.ABC):
    """Identity common to every provider ABC."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable short identifier used as the provider's config-key value.

        Lowercase, no spaces (hyphens allowed where they preserve an existing
        user-visible name). Registries key providers by this string.
        """

    @property
    def display_name(self) -> str:
        """Human-readable label. Defaults to ``name``."""
        return self.name
