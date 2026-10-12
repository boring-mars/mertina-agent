"""Shared helpers for tool backend selection."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from utils import is_truthy_value

logger = logging.getLogger(__name__)
_DEFAULT_BROWSER_PROVIDER = "local"
_DEFAULT_MODAL_MODE = "auto"
_VALID_MODAL_MODES = {"auto", "direct", "managed"}


def managed_nous_tools_enabled(*, force_fresh: bool = False) -> bool:
    """Coarse gate: entitled to the Nous Tool Gateway (paid Portal access OR a live free
    pool). Fails closed on unknown/error — never blocks startup. Callers narrow per category
    via ``tool_gateway_entitled_for``; ``force_fresh`` is for flows needing a just-bought grant."""
    try:
        from hermes_cli.nous_account import get_nous_portal_account_info
        account_info = (get_nous_portal_account_info(force_fresh=True) if force_fresh
                        else get_nous_portal_account_info())
        return bool(account_info.logged_in) and account_info.tool_gateway_entitled
    except Exception:
        return False






def coerce_modal_mode(value: object | None) -> str:
    """Return the requested modal mode when valid, else the default."""
    mode = str(value or _DEFAULT_MODAL_MODE).strip().lower()
    return mode if mode in _VALID_MODAL_MODES else _DEFAULT_MODAL_MODE


normalize_modal_mode = coerce_modal_mode


















# Provider value the managed "Nous Subscription" picker rows write for every category;
# any other name = that vendor direct; no key = legacy autodetect.
NOUS_MANAGED_PROVIDER = "nous"
# Per-capability keys that also count as "this category has been configured".
_EXTRA_SELECTION_KEYS = {"web": ("search_backend", "extract_backend")}
# Key(s) carrying the category's provider selection. ``browser.backend`` is the DRIVER
# choice (browser-use CLI vs built-in), not the cloud provider — excluded.
_SELECTION_NAME_KEYS = {"browser": ("cloud_provider",), "web": ("backend",)}
_DEFAULT_NAME_KEYS = ("provider", "backend", "cloud_provider")


def _raw_section(section: str) -> Dict[str, Any] | None:
    """The RAW (unmerged) config.yaml mapping for ``section``, or None."""
    try:
        from hermes_cli.config import read_raw_config_readonly
        cfg = read_raw_config_readonly() or {}
        raw = cfg.get(section) if isinstance(cfg, dict) else None
        return raw if isinstance(raw, dict) else None
    except Exception:
        return None


def read_selection(section: str) -> str | None:
    """THE single runtime read of the persisted `hermes tools` selection: ``"nous"`` (managed
    gateway row), a vendor name (direct, own credentials), or ``None`` (never configured ->
    legacy autodetect allowed). Reads the RAW config.yaml so key presence means "actually
    written", not "schema default"; a raw ``local`` is therefore a real user selection.
    Legacy shim: ``use_gateway: true`` was only ever written by the managed row, so it maps
    to ``"nous"`` regardless of the name key. Never raises."""
    raw = _raw_section(section)
    if raw is None:
        return None
    if is_truthy_value(raw.get("use_gateway")):
        return NOUS_MANAGED_PROVIDER
    for key in _SELECTION_NAME_KEYS.get(section, _DEFAULT_NAME_KEYS):
        text = str(raw.get(key)).strip().lower() if raw.get(key) is not None else ""
        if text:
            return text
    # use_gateway: false with no name key is not a usable selection shape;
    # per-capability web keys still count as configured via selection_exists().
    return None


def selection_exists(section: str) -> bool:
    """True when ANY selection signal was ever written for the section (wider than
    read_selection: per-capability web keys count too)."""
    if read_selection(section) is not None:
        return True
    extra = _EXTRA_SELECTION_KEYS.get(section, ())
    raw = _raw_section(section) if extra else None
    return raw is not None and any(str(raw.get(key) or "").strip() for key in extra)


# Backends that once shipped in-tree but were removed; a config still pointing at one would
# otherwise fail silently at the FIRST tool call with a generic "no registered provider has that
# name". Used by the startup config check and selection_error(); add removals here, never as
# one-off string checks:  "web": {"<name>": "the <Name> backend was removed in vX (...)"}
REMOVED_BACKENDS: Dict[str, Dict[str, str]] = {}


# Backends that once shipped in-tree but were removed. A config that still points at one otherwise fails
# silently at the FIRST tool call with a generic "no registered provider has that name" — no migration, no
# startup notice (reported after the Tavily removal in #99199). Both the startup config check
# (hermes_cli.config.validate_config_structure) and selection_error() consult this map so the user learns
# what actually happened and what to do. Declared data, one policy — add future removals here, never as
# one-off string checks at call sites.
# Currently empty: the Tavily removal (#99199) that introduced this registry was reverted by the #99731
# restore. Future backend removals add an entry here, e.g. "web": {"<name>": "the <Name> backend was removed
# in vX.Y.Z (...)"},
def removed_backend_note(section: str, name: str) -> Optional[str]:
    """Explanation for a backend that used to ship in-tree, or None. ``name`` tolerates the
    quoted form callers pass to selection_error()."""
    return REMOVED_BACKENDS.get(section, {}).get((name or "").strip().strip("'\"").lower())


def selection_error(section: str, selection_name: str, failure: str) -> str:
    """The uniform honest-error contract for a selected-but-broken provider."""
    failure = removed_backend_note(section, selection_name) or failure
    return (f"{section} is configured to use {selection_name} (set via hermes "
            f"tools), but {failure}. Run 'hermes tools' to change it.")
