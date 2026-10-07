# Ported from hermes-agent hermes_cli/plugins_discovery.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Plugin discovery: directory scanning and the enable/disable gate.

Split out of :mod:`mertina.cli.plugins`. Names that tests patch on the origin
(``get_bundled_plugins_dir``) are looked up lazily through it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from mertina.cli.config import cfg_get
from mertina.cli.plugins_manifest import (
    PluginManifest,
    manifest_key,
    parse_manifest_file,
)

logger = logging.getLogger("mertina.cli.plugins")


def _get_disabled_plugins() -> set:
    """Read ``plugins.disabled`` — a deny-list that wins over ``plugins.enabled``."""
    try:
        from mertina.cli.config import load_config

        disabled = cfg_get(load_config(), "plugins", "disabled", default=[])
        return set(disabled) if isinstance(disabled, list) else set()
    except Exception:
        return set()


def _get_enabled_plugins() -> set | None:
    """Read the ``plugins.enabled`` allow-list (plugins are opt-in). ``None`` = key
    missing/malformed ("nothing enabled yet"; the first ``migrate_config`` run grandfathers
    installed user plugins); ``set()`` = explicitly empty; else the allow-list."""
    try:
        from mertina.cli.config import load_config

        enabled = cfg_get(load_config(), "plugins", "enabled")
        return set(enabled) if isinstance(enabled, list) else None
    except Exception:
        return None


def scan_directory(
    path: Path, source: str, *, skip_names: set[str] | None = None, prefix: str = "", depth: int = 0
) -> list[PluginManifest]:
    """Read manifests under *path*: flat ``<root>/<name>/plugin.yaml`` (key ``name``) or category
    ``<root>/<cat>/<name>/plugin.yaml`` (key ``cat/name``; a manifest-less directory recurses one
    level, depth capped at two). *skip_names* ignores top-level names."""
    manifests: list[PluginManifest] = []
    if not path.is_dir():
        return manifests
    for child in sorted(path.iterdir()):
        try:
            if not child.is_dir() or (depth == 0 and skip_names and child.name in skip_names):
                continue
            manifest_file = next(
                (f for f in (child / "plugin.yaml", child / "plugin.yml") if f.exists()), None
            )
        except OSError as exc:
            # stat() raises (not "False") on an unsearchable directory — Windows ACLs (WinError 5)
            # or a mode-000 dir; one such plugin must not abort discovery for every other plugin
            # (#111804).
            logger.warning("Skipping unreadable plugin directory %s: %s", child, exc)
            continue
        if manifest_file is not None:
            manifest = parse_manifest_file(manifest_file, child, source, prefix)
            if manifest is not None:
                manifests.append(manifest)
        elif depth >= 1:
            logger.debug("Skipping %s (no plugin.yaml, depth cap reached)", child)
        else:
            sub_prefix = f"{prefix}/{child.name}" if prefix else child.name
            manifests.extend(scan_directory(child, source, prefix=sub_prefix, depth=depth + 1))
    return manifests


def collect_directory_manifests() -> list[PluginManifest]:
    """Read the bundled directory manifests without loading or mutating anything."""
    from mertina.cli import plugins as _origin  # patched names resolve through the origin

    manifests: list[PluginManifest] = []

    def _scan(label: str, directory: Path, source: str, skip_names: set[str] | None = None) -> None:
        found = scan_directory(directory, source, skip_names=skip_names)
        logger.debug("  %s: %d manifest(s)", label, len(found))
        manifests.extend(found)

    # Excluded bundled top-level categories have their own discovery.
    repo_plugins = _origin.get_bundled_plugins_dir()
    logger.debug("Scanning bundled plugins: %s", repo_plugins)
    _scan(
        "bundled (top-level)",
        repo_plugins,
        "bundled",
        {"memory", "context_engine", "platforms", "model-providers"},
    )
    return manifests


@dataclass(frozen=True)
class ManifestGate:
    """Routing verdict for one winning manifest (see :func:`gate_manifest`)."""

    action: str  # "load" (dependency-ordered pass) | "placeholder" | "load_now"
    enabled: bool = False
    error: str | None = None
    log: tuple | None = None  # (level, message, *args)


def gate_manifest(
    manifest: PluginManifest, disabled: set[str], enabled: set[str] | None
) -> ManifestGate:
    """Decide how one winning manifest is handled. Gate order matters: explicit disable, bundled
    backend auto-load, then ``plugins.enabled`` opt-in (path-derived key or legacy bare name)."""
    lookup_key = manifest_key(manifest)
    names = {lookup_key, manifest.name}

    def _placeholder(
        error: str | None, level: int, message: str, *args, enabled: bool = False
    ) -> ManifestGate:
        return ManifestGate(
            "placeholder", enabled=enabled, error=error, log=(level, message, lookup_key, *args)
        )

    if names & disabled:
        return _placeholder("disabled via config", logging.DEBUG, "Skipping disabled plugin '%s'")
    if manifest.source == "bundled":
        # Bundled backends auto-load; selection among them is ``<category>.provider`` config.
        if manifest.kind == "backend":
            return ManifestGate("load_now")
    if enabled is None or not names & enabled:
        return _placeholder(
            f"not enabled in config (run `mertina plugins enable {lookup_key}` to activate)",
            logging.DEBUG,
            "Skipping '%s' (not in plugins.enabled)",
        )
    return ManifestGate("load")
