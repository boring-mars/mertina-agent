# Ported from hermes-agent hermes_cli/plugins_ledger.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Registration ownership ledger: every plugin registration is recorded with its inverse. Mixed into
:class:`mertina.cli.plugins.PluginManager`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mertina.cli.plugins_manifest import PluginManifest, manifest_key
from mertina.registration_lifecycle import replacement_coordinator


@dataclass
class PluginRegistration:
    """One host-owned registration plus its inverse, so force reload unwinds registries in reverse
    order (including restoring the entry an override replaced)."""

    kind: str
    key: str
    release: Callable[[], None]
    plugin_key: str = ""
    # Process-global host infrastructure (e.g. dashboard-auth providers): kept out of
    # ``_registration_order`` so unload-all cannot dispose it, but still disposed by a *targeted*
    # unload and evicted on force re-discovery when the plugin no longer re-registers it. See
    # #91701.
    persistent: bool = False
    _disposed: bool = field(default=False, init=False, repr=False)
    _on_dispose: Callable[[PluginRegistration], None] | None = field(
        default=None, init=False, repr=False
    )

    @property
    def active(self) -> bool:
        """Whether this handle still owns an active registration."""
        return not self._disposed

    def dispose(self) -> None:
        """Release this registration once; repeated disposal is harmless."""
        if self._disposed:
            return
        self._disposed = True
        try:
            self.release()
        finally:
            if self._on_dispose is not None:
                self._on_dispose(self)


class PluginLedgerMixin:
    def _track_registration(
        self,
        manifest: PluginManifest,
        kind: str,
        key: str,
        release: Callable[[], None],
        *,
        persistent: bool = False,
    ) -> PluginRegistration:
        """Record one registration under its canonical plugin key. ``persistent`` ones
        (process-global host infrastructure) stay in the ownership ledger for attribution but NOT in
        ``_registration_order``, so a routine unload cannot dispose them; the handle still releases
        on explicit ``dispose()``.

        See #91701."""
        registration = PluginRegistration(
            kind=kind,
            key=key,
            release=release,
            plugin_key=manifest_key(manifest),
            persistent=persistent,
        )
        self._ownership_ledger.setdefault(registration.plugin_key, []).append(registration)
        if not persistent:
            self._registration_order.append(registration)
        return registration

    def _track_scoped_registration(
        self,
        manifest: PluginManifest,
        kind: str,
        name: str,
        registry: Any,
        current: Any,
        previous: Any,
        *,
        finalize: Callable[[], None] | None = None,
    ) -> PluginRegistration:
        """Lease one ``(kind, scope, name)`` slot of a scope-keyed process-global registry. Unload
        calls ``registry.restore_registration(name, current, replacement, scope=...)`` —
        identity-conditional, so a later generation is never removed by an earlier owner."""
        scope = self.scope_key
        lease = replacement_coordinator.acquire(
            (kind, scope, name),
            current=current,
            previous=previous,
            restore=lambda replacement: registry.restore_registration(
                name, current, replacement, scope=scope
            ),
            finalize=finalize,
        )
        return self._track_registration(manifest, kind, name, lease.dispose)
