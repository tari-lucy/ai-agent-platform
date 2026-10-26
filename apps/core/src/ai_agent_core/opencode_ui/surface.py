"""Authenticated, deny-by-default OC-01 adapter to the existing owner HTTP boundary.

This is the early bridge, not a proxy to the native OpenCode server. No native
provider, shell, config, filesystem, permission or prompt endpoint is exposed.
The upstream UI/SDK compatibility adapter is the next consumer (OC-03).
"""

import re
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from ai_agent_contracts.agent_sessions import WorkSession
from ai_agent_contracts.api import Page
from ai_agent_contracts.catalogue import ADAPTERS
from ai_agent_contracts.codec import ContractError, from_wire
from ai_agent_contracts.common import MAX_BODY_BYTES, Scope
from ai_agent_contracts.persona import Profile
from ai_agent_contracts.states import ErrorCode
from pydantic import TypeAdapter
from starlette.responses import JSONResponse, Response
from starlette.types import Message, Receive
from starlette.types import Scope as ASGIScope

from ai_agent_core.access.sessions import SessionAuthority
from ai_agent_core.access.surface import Rejection, _body, _headers, _json, _octet

from .protocol import COMMANDS, PREFIX, AgentCard, Catalogue, SessionCard, SessionPage, directory

if TYPE_CHECKING:
    from ai_agent_core.access.surface import Surface

FIELDS = ("workspace_id", "persona_id", "memory_space_id", "memory_mode")
SCOPED = re.compile(re.escape(PREFIX) + r"/agents/(aw_[a-f0-9]{64})/(.+)")
READS = {
    "sessions": "work-sessions",
    "run": "sessions",
    "files": "workplace-library",
}
DETAIL = re.compile(r"(sessions|files|operations)/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})")
# W4: the persona's home volume. Logical path in the URL (the query keys are fixed).
HOME = re.compile(r"(home|home-file|home-inbox)(?:/(.*))?")
HOME_UPLOAD_MAX = 32 * 1024 * 1024


async def _home(
    surface: Surface,
    asgi: ASGIScope,
    receive: Receive,
    authority: SessionAuthority,
    scope: Scope,
    method: str,
    kind: str,
    path: str | None,
) -> Response:
    """List/read read-only; the one write is a new operator file in inbox/."""
    if surface.ports.home is None:
        raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
    if surface.query(asgi, reading=method == "GET"):
        raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
    path = path or ""
    if kind == "home-inbox":
        if method != "POST" or not path:
            raise Rejection(404, ErrorCode.INVALID_ARGUMENTS)
        data = await _octet(_headers(asgi), receive, HOME_UPLOAD_MAX)
        surface.sessions.check_scope(authority, scope)  # expiry during the upload
        result = await surface.ports.home.put(scope, path, data)
    elif method != "GET":
        raise Rejection(404, ErrorCode.INVALID_ARGUMENTS)
    elif kind == "home":
        result = await surface.ports.home.list(scope, path)
    else:
        result = await surface.ports.home.read(scope, path)
    surface.sessions.check_scope(authority, scope)
    if result is None:
        raise Rejection(404, ErrorCode.INVALID_ARGUMENTS)
    return JSONResponse(
        result, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    )


async def _read(
    surface: Surface,
    asgi: ASGIScope,
    authority: SessionAuthority,
    scope: Scope,
    resource: str,
    identifier: str | None = None,
    *,
    paged: bool = False,
) -> Response:
    path = "/api/v1/" + resource + ("/" + identifier if identifier is not None else "")
    query = dict(zip(FIELDS, scope.partition, strict=True))
    incoming = surface.query(asgi, reading=True)
    if set(incoming) - ({"limit", "cursor"} if paged else set()):
        raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
    query.update(incoming)
    translated = {**asgi, "path": path, "query_string": urlencode(query).encode("ascii")}
    return await surface.read(path, translated, authority)


async def dispatch(
    surface: Surface,
    asgi: ASGIScope,
    receive: Receive,
    authority: SessionAuthority,
) -> Response:
    """Called only after the existing Host/Origin/cookie/CSRF/request-limit boundary."""
    path, method = asgi["path"], asgi["method"]
    if path == PREFIX + "/manifest" and method == "GET":
        if surface.query(asgi, reading=True):
            raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
        return JSONResponse(
            {
                "protocol": "aw-opencode/1",
                "native_version": "1.19.3",
                "native_routes_exposed": False,
                "commands": sorted(COMMANDS),
                "events": "/api/v1/events",
                "events_transport": "websocket",
                "events_sse": "/api/v1/opencode/agents/{directory}/events",
                "ui_base": "/opencode/",
                "stop": "/api/v1/agents/{agent_id}/stop",
                "schema": "docs/protocols/opencode-bridge-v1.md",
            },
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    # The operator session has a bounded, trusted set (at most 8), not a client scope.
    allowed = surface.sessions.info(authority).scopes
    if path == PREFIX + "/agents" and method == "GET":
        if surface.query(asgi, reading=True):
            raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
        items = []
        for scope in allowed:
            try:
                response = await _read(
                    surface, asgi, authority, scope, "personas", scope.persona_id
                )
            except Rejection as error:
                if error.status != 404:
                    raise
                continue  # a preallocated TEST area need not have a profile yet
            profile = from_wire(
                TypeAdapter(Profile), bytes(response.body), extensions=surface.ports.extensions
            )
            items.append(
                AgentCard(
                    directory=directory(scope),
                    scope=scope,
                    profile_id=profile.id,
                    profile_version=profile.version,
                    label=scope.persona_id,
                    active_runtime=surface.supervisor.snapshot.scope.partition == scope.partition,
                )
            )
        surface.sessions.current(authority)  # logout/expiry during the awaited reads
        return _json(Catalogue(items=items))
    match = SCOPED.fullmatch(path)
    if match is None:
        raise Rejection(404, ErrorCode.INVALID_ARGUMENTS)
    key, action = match.groups()
    selected = next((s for s in allowed if directory(s) == key), None)
    if selected is None:
        raise ContractError(ErrorCode.ACCESS_DENIED)
    surface.sessions.check_scope(authority, selected)
    if method == "POST" and action == "commands":
        raw = await _body(
            _headers(asgi),
            receive,
            MAX_BODY_BYTES,
        )
        request = from_wire(ADAPTERS["PanelCommand"], raw, extensions=surface.ports.extensions)
        if request.type not in COMMANDS or request.scope.partition != selected.partition:
            raise ContractError(ErrorCode.ACCESS_DENIED)

        # No synthesis of new request IDs, current versions, run scopes or ACK revisions.
        # Re-enter the same owner command boundary with the exact body and credentials.
        async def body() -> Message:
            return {"type": "http.request", "body": raw, "more_body": False}

        return await surface.http({**asgi, "path": "/api/v1/commands"}, body)
    home = HOME.fullmatch(action)
    if home is not None:
        return await _home(surface, asgi, receive, authority, selected, method, *home.groups())
    if method != "GET":
        raise Rejection(404, ErrorCode.INVALID_ARGUMENTS)
    if action in READS:
        response = await _read(
            surface, asgi, authority, selected, READS[action], paged=action in ("sessions", "run")
        )
        if action != "sessions" or response.status_code != 200:
            return response
        page = Page[WorkSession].model_validate_json(bytes(response.body))
        return _json(
            SessionPage(
                items=[
                    SessionCard.model_validate(
                        item.model_dump(include=set(SessionCard.model_fields))
                    )
                    for item in page.items
                ],
                limit=page.limit,
                next_cursor=page.next_cursor,
                complete=page.complete,
                projection_lag_ms=page.projection_lag_ms,
            )
        )
    detail = DETAIL.fullmatch(action)
    if detail is not None:
        kind, identifier = detail.groups()
        resource = {
            "sessions": "work-session-transcript",
            "files": "workplace-file",
            "operations": "operations",
        }[kind]
        return await _read(surface, asgi, authority, selected, resource, identifier)
    raise Rejection(404, ErrorCode.INVALID_ARGUMENTS)
