"""Optional static panel from an already built `apps/panel/dist`, on the API's own address.

Only regular files below the validated build directory are served. `/api/*` and `/internal/*`
never reach this handler (the access layer answers them), so an unknown API path stays a JSON
error. Extensionless paths fall back to `index.html` for page navigation; a missing file with
an extension is a plain 404.
"""

import re
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse

from .layout import HostError

MAX_INDEX_BYTES = 256 * 1024
LOCAL_REFERENCE = re.compile(r"""(?:src|href)\s*=\s*["'](/[^"'?#]+)["']""")
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def validate(panel_dir: Path, root: Path) -> Path:
    """Return the resolved build directory or raise HostError with a fixed code."""
    if not panel_dir.is_absolute():
        raise HostError("PANEL_DIR_NOT_ABSOLUTE")
    if not panel_dir.is_dir():
        raise HostError("PANEL_DIR_MISSING")
    build = panel_dir.resolve()
    host = root.resolve()
    if _inside(build, host) or _inside(host, build):
        raise HostError("PANEL_DIR_OVERLAPS_HOST")  # data and secrets must stay unreachable
    index = build / "index.html"
    if not index.is_file() or index.is_symlink():
        raise HostError("PANEL_BUILD_INCOMPLETE")
    try:
        with index.open("rb") as handle:
            raw = handle.read(MAX_INDEX_BYTES + 1)
        page = raw.decode("utf-8")
    except OSError, UnicodeDecodeError:
        raise HostError("PANEL_BUILD_INCOMPLETE") from None
    if len(raw) > MAX_INDEX_BYTES or '<div id="root"' not in page:
        raise HostError("PANEL_BUILD_INCOMPLETE")
    references = LOCAL_REFERENCE.findall(page)
    if not references:
        raise HostError("PANEL_BUILD_INCOMPLETE")
    for reference in references:
        target = _resolve(build, reference[1:])
        if target is None:
            raise HostError("PANEL_BUILD_INCOMPLETE")
    return build


def _resolve(build: Path, relative: str) -> Path | None:
    parts = relative.split("/")
    if any(not part or part.startswith(".") or "\\" in part or "\0" in part for part in parts):
        return None
    candidate = build.joinpath(*parts)
    try:
        real = candidate.resolve(strict=True)
    except OSError:
        return None
    if not _inside(real, build) or not real.is_file():
        return None
    return real


def mount(app: FastAPI, build: Path) -> None:
    index = build / "index.html"

    async def serve(request: Request, path: str) -> Response:
        if path.split("/", 1)[0] == "opencode":
            # The old OpenCode window through the platform is gone (doc33): no page navigation.
            return PlainTextResponse("", status_code=404, headers=HEADERS)
        if request.method not in ("GET", "HEAD"):
            return PlainTextResponse("", status_code=405, headers=HEADERS)
        if any(part.startswith(".") for part in path.split("/") if part):
            return PlainTextResponse("", status_code=404, headers=HEADERS)
        if path == "" or "." not in path.rsplit("/", 1)[-1]:
            # Page navigation. Dot-segments in the path are not navigation.
            if any(part in ("..", ".") for part in path.split("/")):
                return PlainTextResponse("", status_code=404, headers=HEADERS)
            return FileResponse(index, headers={**HEADERS, "Cache-Control": "no-store"})
        target = _resolve(build, path)
        if target is None:
            return PlainTextResponse("", status_code=404, headers=HEADERS)
        return FileResponse(target, headers=HEADERS)

    app.add_api_route(
        "/{path:path}",
        serve,
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
