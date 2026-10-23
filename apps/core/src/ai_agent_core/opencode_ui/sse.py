"""Bounded owner-event notifications, independent of inference and the native server."""

import asyncio
import json
import re
from typing import TYPE_CHECKING

from ai_agent_contracts.api import EventSubscription, PanelEvent
from ai_agent_contracts.catalogue import ADAPTERS
from ai_agent_contracts.codec import ContractError, from_wire
from ai_agent_contracts.states import ErrorCode
from starlette.types import Receive, Send
from starlette.types import Scope as ASGIScope

from ai_agent_core.access.events import Subscriber
from ai_agent_core.access.surface import STATUS, Rejection, _error, _headers

from .protocol import PREFIX, directory, invalidation

if TYPE_CHECKING:
    from ai_agent_core.access.surface import Surface

PATH = re.compile(re.escape(PREFIX) + r"/agents/(aw_[a-f0-9]{64})/events")


def event_path(path: str) -> bool:
    return PATH.fullmatch(path) is not None


async def stream(surface: Surface, scope: ASGIScope, receive: Receive, send: Send) -> None:
    """Shares Events' client/queue ceiling with WS, not normal requests or direct STOP.

    No replay store exists. Every connection starts with an explicit refetch request;
    Last-Event-ID never means replay, delivery, execution or a resumed model lease.
    """
    client: Subscriber | None = None
    started = False

    async def emit(raw: bytes) -> None:
        async with asyncio.timeout(0.25):
            await send({"type": "http.response.body", "body": raw, "more_body": True})

    try:
        if scope["method"] != "GET":
            raise Rejection(405, ErrorCode.INVALID_ARGUMENTS)
        surface.query(scope)  # no tokens, arbitrary scopes or replay cursor in the URL
        headers = _headers(scope)
        authority = surface.operator(headers, mutation=False)
        matched = PATH.fullmatch(scope["path"])
        assert matched is not None
        selected = next(
            (
                s
                for s in surface.sessions.info(authority).scopes
                if directory(s) == matched.group(1)
            ),
            None,
        )
        if selected is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        surface.sessions.check_scope(authority, selected)
        client = surface.events.subscribe(
            EventSubscription(scope=selected), notifications_only=True
        )
        async with asyncio.timeout(0.25):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/event-stream; charset=utf-8"),
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-accel-buffering", b"no"),
                    ],
                }
            )
        started = True
        ready = json.dumps(
            {"protocol": "aw-opencode/1", "directory": directory(selected), "replay": "refetch"},
            separators=(",", ":"),
        ).encode()
        surface.sessions.check_scope(authority, selected)
        await emit(b"event: connected\ndata: " + ready + b"\n\n")
        heartbeat = asyncio.get_running_loop().time()
        while True:
            surface.sessions.check_scope(authority, selected)
            if client.closed:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            while client.buffer:
                event = from_wire(ADAPTERS["StreamEvent"], client.buffer.popleft())
                # Model fragments are not a finished reply, cannot be ACKed and may have
                # withdrawn bases. This transport contains no model/private text at all.
                if not isinstance(event, PanelEvent):
                    continue
                notice = invalidation(event, selected)
                if notice is not None:
                    surface.sessions.check_scope(authority, selected)
                    await emit(
                        b"id: "
                        + notice.event_id.encode()
                        + b"\nevent: refetch\ndata: "
                        + notice.model_dump_json().encode()
                        + b"\n\n"
                    )
            if asyncio.get_running_loop().time() - heartbeat >= 15:
                surface.sessions.check_scope(authority, selected)
                await emit(b": keepalive\n\n")
                heartbeat = asyncio.get_running_loop().time()
            try:
                async with asyncio.timeout(0.1):
                    message = await receive()
                if message["type"] == "http.disconnect":
                    break
                if message["type"] != "http.request" or message.get("body"):
                    raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
            except TimeoutError:
                continue
    except (ContractError, Rejection) as error:
        code = error.error.code if isinstance(error, ContractError) else error.code
        if not started:
            status = STATUS.get(code, 400) if isinstance(error, ContractError) else error.status
            await _error(status, code)(scope, receive, send)
            return
        try:
            await emit(
                b"event: closed\ndata: " + json.dumps({"code": code.value}).encode() + b"\n\n"
            )
        except TimeoutError:
            pass
    except TimeoutError, OSError:
        if not started:
            await _error(503, ErrorCode.SERVICE_UNAVAILABLE)(scope, receive, send)
            return
    except Exception:
        # Never pass raw internal event/transport exceptions to the browser or a log.
        if not started:
            await _error(503, ErrorCode.SERVICE_UNAVAILABLE)(scope, receive, send)
            return
    finally:
        if client is not None:
            surface.events.remove(client)
    if started:
        try:
            async with asyncio.timeout(0.25):
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except TimeoutError, OSError:
            pass
