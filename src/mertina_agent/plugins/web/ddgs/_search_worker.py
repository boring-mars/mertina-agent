"""DDGS search child-process entrypoint.

Run as ``python -m mertina_agent.plugins.web.ddgs._search_worker``. Reads one JSON
request ``{"query": str, "safe_limit": int}`` from stdin, writes one envelope
``{"ok": true, "results": [...]}`` or ``{"ok": false, "error": str}`` to stdout
and exits.

Copied from Hermes plugins/web/ddgs/_search_worker.py at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt. The test hooks are left out: tests
substitute the whole worker command instead. Running as a module of the
installed package removes Hermes's ``PYTHONPATH`` workaround.
"""

import json
import sys


def _write_envelope(envelope: dict[str, object]) -> None:
    json.dump(envelope, sys.stdout)
    sys.stdout.flush()


def _fail(error: str, code: int) -> int:
    _write_envelope({"ok": False, "error": error})
    return code


def main() -> int:
    """Serve one search request; the exit code mirrors the envelope's ``ok``."""
    try:
        request = json.load(sys.stdin)
        query = str(request.get("query") or "")
        safe_limit = max(1, int(request.get("safe_limit") or 1))
    except (ValueError, TypeError, AttributeError) as exc:
        return _fail(f"invalid request: {exc}", 2)
    try:
        # Imported lazily so a malformed request fails before ddgs loads.
        from mertina_agent.plugins.web.ddgs.provider import _run_ddgs_search

        _write_envelope({"ok": True, "results": _run_ddgs_search(query, safe_limit)})
    except Exception as exc:
        # The child's whole job is to report the failure through its envelope.
        return _fail(f"{type(exc).__name__}: {exc}", 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
