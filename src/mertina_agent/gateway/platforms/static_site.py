"""Serve the built Web UI from the Gateway's own origin, with an SPA fallback.

Static files are served only for GET and HEAD requests that no API route
matched and whose path is outside ``/api/`` and ``/v1/``, so every API miss
keeps its JSON error envelope. Requests still pass the local Host/Origin
boundary first, because the middleware runs inside it.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

BUILD_COMMAND = "npm --prefix web run build"
"""Command that produces ``web/dist``; shown when the UI has not been built."""

API_PREFIXES = ("/api/", "/v1/")

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "font-src 'self'",
        "img-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)
"""Same-origin only: no inline scripts, remote assets or framing."""

type Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def default_static_dir() -> Path:
    """Return ``web/dist`` of the source checkout this package runs from."""
    return Path(__file__).resolve().parents[4] / "web" / "dist"


def _is_api(path: str) -> bool:
    return path in ("/api", "/v1") or path.startswith(API_PREFIXES)


def _resolve(root: Path, path: str) -> tuple[Path, bool] | None:
    """Map a URL path to ``(file, is_fallback)``, or ``None`` for a 404.

    Paths with dot segments, hidden names, backslashes or NUL are refused
    before touching the file system; symbolic links may not leave ``root``.
    A missing path whose last segment has no extension is a client-side route
    and falls back to ``index.html``; a missing asset stays a 404, so a stale
    script URL never receives HTML.
    """
    segments = [segment for segment in path.split("/") if segment]
    if any(segment.startswith(".") or "\\" in segment or "\x00" in segment for segment in segments):
        return None
    base = root.resolve()
    index = base / "index.html"
    if not index.is_file():
        return None
    candidate = base.joinpath(*segments).resolve()
    if candidate.is_relative_to(base) and candidate.is_file():
        return candidate, candidate == index
    if segments and "." in segments[-1]:
        return None
    return index, True


class StaticSite:
    """Middleware that serves one build directory without owning any resources."""

    def __init__(self, root: Path) -> None:
        """Serve ``root``; a missing build is logged once and served as 404s."""
        self.root = root
        if not (root / "index.html").is_file():
            logger.warning(
                "Web UI is not built; API routes still work. Run `%s` to build it.",
                BUILD_COMMAND,
            )

    @web.middleware
    async def middleware(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        """Answer unmatched non-API GET/HEAD requests from the build directory."""
        unmatched = isinstance(request.match_info.http_exception, web.HTTPNotFound)
        if not unmatched or request.method not in ("GET", "HEAD") or _is_api(request.path):
            return await handler(request)
        resolved = await asyncio.to_thread(_resolve, self.root, request.path)
        if resolved is None:
            raise web.HTTPNotFound()
        file, fallback = resolved
        response = web.FileResponse(file)
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if fallback:
            # Asset names are content-hashed; only the entry page must revalidate.
            response.headers["Cache-Control"] = "no-cache"
        return response
