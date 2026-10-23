"""Immutable allowlisted frontend bundle on the platform origin, without a native API proxy.

Bundle production/adaptation belongs to OC-03. No build/download, filesystem
endpoint, session cache or OpenCode server is started by mounting these assets.
"""

import base64
import hashlib
import json
import mimetypes
import re
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from types import MappingProxyType

from ai_agent_contracts.states import ErrorCode
from fastapi import FastAPI, Request, Response

from ai_agent_core.access.surface import _error
from ai_agent_core.local_host.layout import HostError

BASE = "/opencode"
MANIFEST = "platform-ui-manifest.json"
MAX_FILES = 512
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*")
PAGE = re.compile(r"[A-Za-z0-9_-]{1,256}/session(?:/[A-Za-z0-9_.:-]{1,128})?")
EXTENSIONS = frozenset(
    {
        ".html",
        ".js",
        ".css",
        ".svg",
        ".png",
        ".jpg",
        ".webp",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".wasm",
        ".json",
        ".webmanifest",
        ".txt",
    }
)
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; "
    "connect-src 'self'; worker-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'; object-src 'none'",
}


class References(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.assets.append(str(values["src"]))
        if (
            tag == "link"
            and values.get("href")
            and set(str(values.get("rel", "")).split())
            & {
                "stylesheet",
                "modulepreload",
                "icon",
                "manifest",
                "preload",
            }
        ):
            self.assets.append(str(values["href"]))


def _name(value: str) -> bool:
    return bool(NAME.fullmatch(value)) and all(not p.startswith(".") for p in value.split("/"))


def _page(value: str) -> bool:
    if PAGE.fullmatch(value) is None:
        return False
    project = value.split("/")[0]
    if re.fullmatch(r"aw_[a-f0-9]{64}", project):
        return True
    try:
        virtual = base64.b64decode(
            project + "=" * (-len(project) % 4), altchars=b"-_", validate=True
        ).decode("ascii")
    except ValueError, UnicodeError:
        return False
    return re.fullmatch(r"aw_[a-f0-9]{64}", virtual) is not None


def _read(root: Path, name: str, limit: int) -> bytes:
    if not _name(name):
        raise HostError("OPENCODE_UI_INVALID")
    path = root / name
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents) if p != root):
        raise HostError("OPENCODE_UI_LINK")
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise HostError("OPENCODE_UI_INVALID")
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise HostError("OPENCODE_UI_TOO_LARGE")
    return raw


def _root(build: Path, host: Path | None) -> Path:
    if not build.is_absolute() or not build.is_dir():
        raise HostError("OPENCODE_UI_DIR_INVALID")
    if build.is_symlink() or build.is_junction():
        raise HostError("OPENCODE_UI_LINK")
    root = build.resolve()
    if host is not None and (
        root.is_relative_to(host.resolve()) or host.resolve().is_relative_to(root)
    ):
        raise HostError("OPENCODE_UI_OVERLAPS_HOST")
    return root


def _index(files: Mapping[str, bytes]) -> None:
    try:
        page = References()
        page.feed(files["index.html"].decode("utf-8"))
        if not page.assets:
            raise ValueError()
        for path in page.assets:
            # Claude builds upstream with this public base. Do not rewrite native JS or
            # quietly fetch root/external assets, fallback HTML, config, keys or provider.
            if not path.startswith(BASE + "/") or path[len(BASE) + 1 :] not in files:
                raise ValueError()
    except KeyError, ValueError, UnicodeError:
        raise HostError("OPENCODE_UI_BASE_MISMATCH") from None


@dataclass(frozen=True)
class Bundle:
    files: Mapping[str, bytes]


def load(build: Path, host: Path) -> Bundle:
    """Read a complete manifest once; all subsequent serving uses sealed bytes, no IO."""
    root = _root(build, host)
    try:
        manifest = json.loads(_read(root, MANIFEST, 256 * 1024))
        if set(manifest) != {"upstream_version", "bridge_protocol", "base_path", "files"} or (
            manifest["upstream_version"] != "1.19.3"
            or manifest["bridge_protocol"] != "aw-opencode/1"
            or manifest["base_path"] != BASE
        ):
            raise ValueError()
        specs = manifest["files"]
        if not isinstance(specs, dict) or not 1 <= len(specs) <= MAX_FILES:
            raise ValueError()
        result = {}
        total = 0
        for name, spec in specs.items():
            if not _name(name) or Path(name).suffix not in EXTENSIONS or name == MANIFEST:
                raise ValueError()
            if not isinstance(spec, dict) or set(spec) != {"size", "sha256"}:
                raise ValueError()
            if type(spec["size"]) is not int or not 0 <= spec["size"] <= MAX_FILE:
                raise ValueError()
            total += spec["size"]
            if total > MAX_TOTAL:
                raise HostError("OPENCODE_UI_TOO_LARGE")
            raw = _read(root, name, spec["size"])
            if len(raw) != spec["size"] or hashlib.sha256(raw).hexdigest() != spec["sha256"]:
                raise HostError("OPENCODE_UI_DIGEST_MISMATCH")
            result[name] = raw
        _index(result)
        return Bundle(MappingProxyType(result))
    except ValueError, TypeError, KeyError, OSError:
        raise HostError("OPENCODE_UI_INVALID") from None


def prepare(build: Path) -> Path:
    """Package an already built upstream adapter; no dependency download or overwrite."""
    root = _root(build, None)
    target = root / MANIFEST
    if target.exists():
        raise HostError("OPENCODE_UI_MANIFEST_EXISTS")
    files: dict[str, bytes] = {}
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink() or path.is_junction():
            raise HostError("OPENCODE_UI_LINK")
        if not path.is_file():
            continue
        name = path.relative_to(root).as_posix()
        if not _name(name) or path.suffix not in EXTENSIONS:
            raise HostError("OPENCODE_UI_INVALID")
        if len(files) >= MAX_FILES:
            raise HostError("OPENCODE_UI_TOO_LARGE")
        raw = _read(root, name, MAX_FILE)
        total += len(raw)
        if total > MAX_TOTAL:
            raise HostError("OPENCODE_UI_TOO_LARGE")
        files[name] = raw
    _index(files)
    data = {
        "upstream_version": "1.19.3",
        "bridge_protocol": "aw-opencode/1",
        "base_path": BASE,
        "files": {
            name: {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            for name, raw in sorted(files.items())
        },
    }
    with target.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2) + "\n")
    return target


def mount(app: FastAPI, bundle: Bundle | None) -> None:
    """Call before the diagnostic panel's catch-all; never mount native APIs."""

    async def serve(request: Request, path: str = "") -> Response:
        if bundle is None:
            return _error(503, ErrorCode.CAPABILITY_UNAVAILABLE)
        if request.scope.get("query_string") or "%" in request.scope.get("raw_path", b"").decode(
            "latin-1"
        ):
            return Response(status_code=400, headers=HEADERS)
        name = "index.html" if not path or _page(path) else path
        raw = bundle.files.get(name) if _name(name) else None
        if raw is None:
            return Response(status_code=404, headers=HEADERS)
        kind = {
            ".js": "text/javascript",
            ".css": "text/css",
            ".html": "text/html",
            ".wasm": "application/wasm",
        }.get(Path(name).suffix)
        kind = kind or mimetypes.guess_type(name)[0] or "application/octet-stream"
        return Response(
            b"" if request.method == "HEAD" else raw,
            media_type=kind,
            headers={**HEADERS, "Content-Length": str(len(raw))},
        )

    app.add_api_route(BASE, serve, methods=["GET", "HEAD"], include_in_schema=False)
    app.add_api_route(
        BASE + "/{path:path}", serve, methods=["GET", "HEAD"], include_in_schema=False
    )
