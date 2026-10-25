"""ASGI boundary: direct in-memory STOP, bounded requests, closed owner ports."""

import asyncio
import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from ai_agent_adapters.configuration import BootstrapSettings, operator_origin
from ai_agent_application.control import Supervisor
from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.jobs.owner import JobOwner
from ai_agent_contracts.access import (
    OperatorLogin,
    ServiceClaimReceipt,
    ServiceLeaseReceipt,
)
from ai_agent_contracts.api import (
    CommandBase,
    ControlCommand,
    EventSubscription,
    Page,
    PanelEvent,
    Receipt,
    ServiceClaimCommand,
    ServiceLeaseCommand,
    ServiceResultCommand,
    StopCommand,
)
from ai_agent_contracts.catalogue import ADAPTERS, READ_MODELS
from ai_agent_contracts.codec import ContractError, from_wire, to_wire
from ai_agent_contracts.common import MAX_BODY_BYTES, ArtifactRef, Contract, Scope
from ai_agent_contracts.control import RunSession
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.memory import RecallRequest, RecallResult
from ai_agent_contracts.states import ErrorCode
from fastapi import FastAPI
from pydantic import TypeAdapter, ValidationError
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Send
from starlette.types import Scope as ASGIScope

from .events import Events, Subscriber
from .sessions import COOKIE, AccessLimits, SessionAuthority, Sessions, WorkerGrant


class Reader(Protocol):
    """Must apply current record ACL/source rights/TTL/api_transfer before returning."""

    async def read(
        self,
        resource: str,
        principal_id: str,
        scope: Scope,
        resource_id: str | None,
        limit: int,
        cursor: str | None,
        order: str | None = None,
        utterance_id: str | None = None,
    ) -> Contract | None: ...


class Views(Protocol):
    async def recall(self, principal_id: str, request: RecallRequest) -> RecallResult: ...

    async def read(
        self,
        resource: str,
        principal_id: str,
        scope: Scope,
        identifier: str | None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Contract: ...

    async def validate_profile(
        self, authority: ControlAuthority, request: CommandBase
    ) -> Contract: ...

    async def preview_transfer(
        self, authority: ControlAuthority, request: CommandBase
    ) -> Contract: ...

    async def operator_upload(
        self,
        authority: ControlAuthority,
        scope: Scope,
        area: str,
        name: str,
        title: str,
        media_type: str,
        content: bytes,
        *,
        authorize: Callable[[], None],
        request_id: str,
    ) -> Contract: ...


class Downloads(Protocol):
    async def memory(
        self, principal_id: str, scope: Scope, operation_id: str
    ) -> tuple[ArtifactRef, bytes]: ...

    async def read(
        self, principal_id: str, scope: Scope, report_id: str
    ) -> tuple[ArtifactRef, bytes]: ...


class Mutations(Protocol):
    async def execute(self, authority: ControlAuthority, request: CommandBase) -> Receipt: ...


@dataclass(frozen=True)
class AccessPorts:
    """Explicit trusted composition. Defaults grant no worker capabilities/data access."""

    views: Views | None = None
    mutations: Mutations | None = None
    reader: Reader | None = None
    jobs: JobOwner | None = None
    worker: WorkerGrant | None = None
    extensions: ExtensionRegistry | None = None
    events: Events | None = None
    scopes: tuple[Scope, ...] | None = None
    limits: AccessLimits | None = None
    downloads: Downloads | None = None


class Rejection(Exception):
    def __init__(self, status: int, code: ErrorCode) -> None:
        self.status, self.code = status, code
        super().__init__(code.value)


STATUS = {
    ErrorCode.ACCESS_DENIED: 403,
    ErrorCode.RESOURCE_BUSY: 429,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.CAPABILITY_UNAVAILABLE: 503,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.STALE_CONTEXT: 409,
    ErrorCode.EXPIRED: 409,
    ErrorCode.OUTCOME_UNKNOWN: 409,
}

UPLOAD = "/api/v1/workplace-files/upload"
UPLOAD_KEYS = frozenset(
    {"workspace_id", "persona_id", "memory_space_id", "memory_mode", "area", "name"}
)
UPLOAD_OPTIONAL = frozenset({"title", "media_type"})
UPLOAD_MAX_BYTES = 104_857_600  # the configured per-file ceiling; transport rejects above it


def _headers(scope: ASGIScope) -> dict[str, str]:
    result: dict[str, str] = {}
    raw = scope.get("headers", [])
    if sum(len(k) + len(v) for k, v in raw) > 16384:
        raise Rejection(413, ErrorCode.INVALID_ARGUMENTS)
    for key, value in raw:
        name = key.decode("latin-1").lower()
        # Never fold security headers: duplicate credentials/cookies/origins are ambiguous.
        if name in result and name in (
            "host",
            "origin",
            "authorization",
            "cookie",
            "x-csrf-token",
            "content-length",
            "content-type",
            "content-encoding",
            "x-upload-request-id",
            "sec-fetch-site",
        ):
            raise Rejection(403, ErrorCode.ACCESS_DENIED)
        result[name] = value.decode("latin-1")
    return result


def _cookie(headers: Mapping[str, str]) -> str:
    matches = []
    for item in headers.get("cookie", "").split(";"):
        key, separator, value = item.strip().partition("=")
        if separator and key == COOKIE:
            matches.append(value)
    if len(matches) != 1:
        raise Rejection(401, ErrorCode.ACCESS_DENIED)
    return matches[0]


async def _body(headers: Mapping[str, str], receive: Receive, maximum: int) -> bytes:
    if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
    if "content-encoding" in headers:
        raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
    if "content-length" in headers:
        try:
            size = int(headers["content-length"])
        except ValueError:
            raise Rejection(400, ErrorCode.INVALID_ARGUMENTS) from None
        if not 0 <= size <= maximum:
            raise Rejection(413, ErrorCode.INVALID_ARGUMENTS)
    result = bytearray()
    async with asyncio.timeout(2):
        while True:
            message = await receive()
            if message["type"] != "http.request":
                raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
            chunk = message.get("body", b"")
            if len(result) + len(chunk) > maximum:
                raise Rejection(413, ErrorCode.INVALID_ARGUMENTS)
            result.extend(chunk)
            if not message.get("more_body", False):
                break
    return bytes(result)


async def _octet(headers: Mapping[str, str], receive: Receive, maximum: int) -> bytes:
    """One bounded raw upload body; the caller has already checked origin and session."""
    if headers.get("content-type", "").split(";")[0].strip().lower() != "application/octet-stream":
        raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
    if "content-encoding" in headers:
        raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
    if "content-length" in headers:
        try:
            size = int(headers["content-length"])
        except ValueError:
            raise Rejection(400, ErrorCode.INVALID_ARGUMENTS) from None
        if not 1 <= size <= maximum:
            raise Rejection(413, ErrorCode.INVALID_ARGUMENTS)
    result = bytearray()
    async with asyncio.timeout(120):
        while True:
            message = await receive()
            if message["type"] != "http.request":
                raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
            chunk = message.get("body", b"")
            if len(result) + len(chunk) > maximum:
                raise Rejection(413, ErrorCode.INVALID_ARGUMENTS)
            result.extend(chunk)
            if not message.get("more_body", False):
                break
    if not result:
        raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
    return bytes(result)


def _json(
    model: Contract, extensions: ExtensionRegistry | None = None, status: int = 200
) -> Response:
    return Response(
        to_wire(model, extensions=extensions),
        status_code=status,
        media_type="application/json",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _error(status: int, code: ErrorCode) -> Response:
    # No inbound request ID, body, header, exception or credential is reflected.
    return _json(ContractError(code).error, status=status)


class Surface:
    def __init__(
        self, supervisor: Supervisor, settings: BootstrapSettings, ports: AccessPorts
    ) -> None:
        if ports.jobs is not None and ports.jobs.supervisor is not supervisor:
            raise ValueError("ACCESS_JOB_OWNER")
        self.supervisor, self.ports = supervisor, ports
        self._uploads = asyncio.Semaphore(1)
        self.origin = operator_origin(settings)
        self.host = urlsplit(self.origin).netloc
        self.secure_cookie = self.origin.startswith("https://")
        self.limits = ports.limits or AccessLimits()
        base = supervisor.snapshot.scope
        default = Scope(
            workspace_id=base.workspace_id,
            persona_id=base.persona_id,
            memory_space_id=base.memory_space_id,
            memory_mode=base.memory_mode,
        )
        self.sessions = Sessions(
            ports.scopes or (default,),
            clock=supervisor.clock,
            password=settings.operator_password_hash,
            service_token=settings.worker_service_token,
            worker=ports.worker,
            limits=self.limits,
        )
        if ports.events is not None and ports.events.limits != self.limits:
            raise ValueError("ACCESS_EVENT_LIMITS")
        self.events = ports.events or Events(self.limits)
        self._requests = 0
        self._stops = 0
        self._websockets = 0

    def run_changed(self) -> None:
        run = self.supervisor.snapshot
        identifier = uuid4().hex
        self.events.publish(
            PanelEvent(
                id=identifier,
                event_id=identifier,
                scope=run.scope,
                created_at=run.updated_at,
                occurred_at=run.updated_at,
                type="run.changed",
                resource_id=run.id,
                resource_version=run.version,
                causal_id=None,
                operation_id=None,
            )
        )

    def browser(self, headers: Mapping[str, str], *, mutation: bool) -> None:
        if headers.get("host") != self.host or "authorization" in headers:
            raise Rejection(403, ErrorCode.ACCESS_DENIED)
        origin = headers.get("origin")
        if (mutation and origin != self.origin) or (origin is not None and origin != self.origin):
            raise Rejection(403, ErrorCode.ACCESS_DENIED)
        if headers.get("sec-fetch-site", "none") not in ("none", "same-origin"):
            raise Rejection(403, ErrorCode.ACCESS_DENIED)

    def operator(self, headers: Mapping[str, str], *, mutation: bool) -> SessionAuthority:
        self.browser(headers, mutation=mutation)
        try:
            authority = self.sessions.authorize(_cookie(headers))
        except ContractError:
            raise Rejection(401, ErrorCode.ACCESS_DENIED) from None
        if mutation:
            self.sessions.csrf(authority, headers.get("x-csrf-token", ""))
        return authority

    def query(self, scope: ASGIScope, *, reading: bool = False) -> dict[str, str]:
        raw = scope.get("query_string", b"")
        if len(raw) > 2048:
            raise Rejection(413, ErrorCode.INVALID_ARGUMENTS)
        try:
            pairs = parse_qsl(raw.decode("ascii"), strict_parsing=True, max_num_fields=8)
        except ValueError, UnicodeError:
            raise Rejection(400, ErrorCode.INVALID_ARGUMENTS) from None
        allowed = {
            "workspace_id",
            "persona_id",
            "memory_space_id",
            "memory_mode",
            "limit",
            "cursor",
            "order",
            "utterance_id",
        }
        result: dict[str, str] = {}
        for key, value in pairs:
            if not reading or key not in allowed or key in result:
                raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
            result[key] = value
        return result

    async def _operator_upload(
        self, asgi: ASGIScope, receive: Receive, headers: Mapping[str, str]
    ) -> Response:
        """One raw operator upload into a workplace area.

        Grants the agent no right and starts nothing; areas are limited by the file owner.
        """
        raw = asgi.get("query_string", b"")
        if len(raw) > 2048:
            raise Rejection(413, ErrorCode.INVALID_ARGUMENTS)
        try:
            pairs = parse_qsl(raw.decode("ascii"), strict_parsing=True, max_num_fields=8)
        except ValueError:
            raise Rejection(400, ErrorCode.INVALID_ARGUMENTS) from None
        fields: dict[str, str] = {}
        for key, value in pairs:
            if key in fields or key not in UPLOAD_KEYS | UPLOAD_OPTIONAL:
                raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
            fields[key] = value
        if set(UPLOAD_KEYS) - set(fields):
            raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
        authority = self.operator(headers, mutation=True)  # Host/Origin/cookie/CSRF as any mutation
        try:
            wanted = Scope.model_validate(
                {
                    key: fields[key]
                    for key in ("workspace_id", "persona_id", "memory_space_id", "memory_mode")
                }
            )
        except ValidationError:
            raise Rejection(400, ErrorCode.INVALID_ARGUMENTS) from None
        selected = next((s for s in self.sessions.info(authority).scopes if s == wanted), None)
        if selected is None or selected.memory_mode != "work":
            raise ContractError(ErrorCode.ACCESS_DENIED)
        self.sessions.check_scope(authority, selected)
        request_id = headers.get("x-upload-request-id", "upload-" + uuid4().hex)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", request_id):
            raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
        content = await _octet(headers, receive, UPLOAD_MAX_BYTES)
        self.sessions.check_scope(authority, selected)
        if self.ports.views is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        entry = await self.ports.views.operator_upload(
            authority.control,
            selected,
            fields["area"],
            fields["name"],
            fields.get("title") or fields["name"],
            fields.get("media_type") or "application/octet-stream",
            content,
            authorize=lambda: self.sessions.check_scope(authority, selected),
            request_id=request_id,
        )
        self.sessions.check_scope(authority, selected)  # expiry during the awaited write
        return _json(entry)

    async def http(self, scope: ASGIScope, receive: Receive) -> Response:
        headers = _headers(scope)
        path, method = scope["path"], scope["method"]
        if path == UPLOAD and method == "POST":
            # Slow uploads have a separate bounded slot and never occupy the STOP path.
            if self._uploads.locked():
                raise Rejection(429, ErrorCode.RESOURCE_BUSY)
            async with self._uploads:
                return await self._operator_upload(scope, receive, headers)
        self.query(scope, reading=method == "GET" and "/auth/" not in path)
        if path.startswith("/internal/"):
            return await self.service(path, method, headers, receive)
        if method == "OPTIONS":
            raise Rejection(403, ErrorCode.ACCESS_DENIED)
        mutation = method not in ("GET", "HEAD")
        self.browser(headers, mutation=mutation)
        if path == "/api/v1/auth/login" and method == "POST":
            # Ignore any previous cookie; mint a new unpredictable session after verification.
            request = from_wire(TypeAdapter(OperatorLogin), await _body(headers, receive, 8192))
            cookie, authority = await self.sessions.login(request.password)
            response = _json(self.sessions.info(authority))
            response.set_cookie(
                COOKIE,
                cookie,
                max_age=self.limits.session_seconds,
                httponly=True,
                secure=self.secure_cookie,
                samesite="strict",
                path="/",
            )
            return response
        authority = self.operator(headers, mutation=mutation)
        if path.startswith("/api/v1/opencode/"):
            from ai_agent_core.opencode_ui.surface import dispatch

            return await dispatch(self, scope, receive, authority)
        if path == "/api/v1/auth/session" and method == "GET":
            return _json(self.sessions.info(authority))
        if path == "/api/v1/auth/logout" and method == "POST":
            self.sessions.logout(authority)
            response = Response(status_code=204, headers={"Cache-Control": "no-store"})
            response.delete_cookie(
                COOKIE, path="/", httponly=True, secure=self.secure_cookie, samesite="strict"
            )
            return response
        if path == "/api/v1/memory/recall" and method == "POST":
            request = from_wire(ADAPTERS["RecallRequest"], await _body(headers, receive, 16384))
            self.sessions.check_scope(authority, request.scope)
            if request.purpose != "api_transfer":
                raise ContractError(ErrorCode.ACCESS_DENIED)
            if len(request.query) > 1000 or request.limit > 20 or not request.query.strip():
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            if self.ports.views is None:
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            recalled = await self.ports.views.recall(authority.control.principal_id, request)
            self.sessions.check_scope(authority, request.scope)
            checked = from_wire(ADAPTERS["RecallResult"], to_wire(recalled))
            if checked.scope != request.scope or len(checked.fragments) > request.limit:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            for fragment in checked.fragments:
                if fragment.source.scope.partition != request.scope.partition:
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                self.sessions.check_scope(authority, fragment.source.scope)
            # Candidate ids can be inaccessible; never expose the recall diagnostic inventory.
            checked = checked.model_copy(update={"missing_source_ids": []})
            return _json(checked)
        if path == "/api/v1/laboratory/transfer-preview" and method == "POST":
            request = from_wire(
                ADAPTERS["EditCommand"],
                await _body(headers, receive, MAX_BODY_BYTES),
                extensions=self.ports.extensions,
            )
            self.sessions.check_scope(authority, request.scope)
            if self.ports.views is None:
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            result = await self.ports.views.preview_transfer(authority.control, request)
            self.sessions.current(authority)
            checked = from_wire(
                ADAPTERS["SettingsTransferPreview"],
                to_wire(result, extensions=self.ports.extensions),
                extensions=self.ports.extensions,
            )
            self.sessions.check_scope(authority, checked.source_scope)
            if checked.scope != request.scope or checked.request_id != request.request_id:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            return _json(checked, self.ports.extensions)
        preview = re.fullmatch(r"/api/v1/personas/([^/]+)/validate", path)
        if method == "POST" and preview:
            request = from_wire(
                ADAPTERS["EditCommand"],
                await _body(headers, receive, MAX_BODY_BYTES),
                extensions=self.ports.extensions,
            )
            self.sessions.check_scope(authority, request.scope)
            if request.type != "persona.validate" or request.target != preview.group(1):
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            if self.ports.views is None:
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            result = await self.ports.views.validate_profile(authority.control, request)
            self.sessions.current(authority)
            return _json(result, self.ports.extensions)
        stop_path = re.fullmatch(r"/api/v1/agents/([^/]+)/stop", path)
        if method == "POST" and (stop_path or path == "/api/v1/commands"):
            raw = await _body(headers, receive, 4096 if stop_path else MAX_BODY_BYTES)
            request = from_wire(
                ADAPTERS["StopCommand" if stop_path else "PanelCommand"],
                raw,
                extensions=self.ports.extensions,
            )
            self.sessions.check_scope(authority, request.scope)
            if stop_path and (
                not isinstance(request, StopCommand) or stop_path.group(1) != request.target
            ):
                raise Rejection(403, ErrorCode.ACCESS_DENIED)
            if isinstance(request, StopCommand):
                # No lock, journal flush, broker, password hash, readiness or model await.
                stopped = self.supervisor.stop(authority.control, request)
                self.run_changed()
                return _json(stopped)
            if isinstance(request, ControlCommand):
                previous = self.supervisor.snapshot.version
                try:
                    receipt = await self.supervisor.control(authority.control, request)
                finally:
                    if self.supervisor.snapshot.version != previous:
                        self.run_changed()
                self.sessions.current(authority)
                return _json(receipt)
            if self.ports.mutations is not None:
                receipt = await self.ports.mutations.execute(authority.control, request)
                self.sessions.current(authority)
                return _json(receipt, self.ports.extensions)
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        if method == "GET":
            return await self.read(path, scope, authority)
        if method == "POST" and path == "/api/v1/recall":
            request = from_wire(
                ADAPTERS["RecallRequest"], await _body(headers, receive, MAX_BODY_BYTES)
            )
            self.sessions.check_scope(authority, request.scope)
            # Recall owner is assigned to M2, not bypassed here.
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        raise Rejection(404, ErrorCode.INVALID_ARGUMENTS)

    async def read(self, path: str, asgi: ASGIScope, authority: SessionAuthority) -> Response:
        download = re.fullmatch(
            r"/api/v1/(laboratory-reports|memory/exports)/"
            r"([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/content",
            path,
        )
        if download is not None:
            if self.ports.downloads is None:
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            query = self.query(asgi, reading=True)
            if set(query) != {"workspace_id", "persona_id", "memory_space_id", "memory_mode"}:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            try:
                selected = Scope.model_validate(query)
            except ValueError:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
            self.sessions.check_scope(authority, selected)
            memory_export = download.group(1) == "memory/exports"
            read = self.ports.downloads.memory if memory_export else self.ports.downloads.read
            reference, body = await read(
                authority.control.principal_id, selected, download.group(2)
            )
            self.sessions.check_scope(authority, selected)
            if (
                reference.scope.partition != selected.partition
                or (not memory_export and selected.memory_mode != "test")
                or reference.media_type != "application/json"
                or not 0 < len(body) <= 2_000_000
                or len(body) != reference.size_bytes
            ):
                raise ContractError(ErrorCode.STALE_CONTEXT)
            if hashlib.sha256(body).hexdigest() != reference.checksum:
                raise ContractError(ErrorCode.STALE_CONTEXT)
            return Response(
                body,
                media_type="application/json",
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Disposition": 'attachment; filename="memory-source.json"'
                    if memory_export
                    else 'attachment; filename="laboratory-report.json"',
                    "ETag": '"' + reference.checksum + '"',
                },
            )
        custom = re.fullmatch(
            r"/api/v1/(api-limits|private-api-status|laboratory-catalogue|laboratory-runs|"
            r"persona-markdown|settings-transfer-status|workplace-library|workplace-access|"
            r"work-session-transcript|work-session-sources|workplace-file)(?:/([^/]+))?",
            path,
        )
        match = re.fullmatch(r"/api/v1/([^/]+)(?:/([^/]+))?", path)
        if match is None or (match.group(1) not in READ_MODELS and custom is None):
            raise Rejection(404, ErrorCode.INVALID_ARGUMENTS)
        query = self.query(asgi, reading=True)
        try:
            scope = Scope.model_validate(
                {
                    key: query[key]
                    for key in ("workspace_id", "persona_id", "memory_space_id", "memory_mode")
                }
            )
            limit = int(query.get("limit", "50"))
            if not 1 <= limit <= 200:
                raise ValueError("LIMIT")
            order = query.get("order")
            utterance_id = query.get("utterance_id")
            resource, identifier = match.groups()
            if order is not None and (
                order != "desc"
                or resource not in {"sources", "utterances", "deliveries", "operations"}
                or identifier is not None
            ):
                raise ValueError("ORDER")
            if utterance_id is not None and (
                resource != "deliveries"
                or identifier is not None
                or order != "desc"
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", utterance_id)
            ):
                raise ValueError("FILTER")
            cursor = query.get("cursor")
            if cursor is not None:
                TypeAdapter(str).validate_python(cursor)
                if len(cursor) > (512 if order == "desc" else 128) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.:-]*", cursor
                ):
                    raise ValueError("CURSOR")
        except ValueError, KeyError:
            raise Rejection(400, ErrorCode.INVALID_ARGUMENTS) from None
        self.sessions.check_scope(authority, scope)
        resource, identifier = match.groups()
        if custom is not None:
            if self.ports.views is None:
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            if resource == "laboratory-runs":
                custom_model = await self.ports.views.read(
                    resource, authority.control.principal_id, scope, identifier, limit, cursor
                )
            else:
                custom_model = await self.ports.views.read(
                    resource, authority.control.principal_id, scope, identifier
                )
            self.sessions.check_scope(authority, scope)
            expected = {
                "api-limits": "ApiLimits",
                "private-api-status": "PrivateApiStatus",
                "laboratory-catalogue": "LaboratoryCatalogue",
                "laboratory-runs": "LaboratoryRuns",
                "persona-markdown": "ProfileMarkdown",
                "settings-transfer-status": "SettingsTransferStatus",
                "workplace-library": "WorkplaceLibrary",
                "workplace-access": "InitialAccessStatus",
                "work-session-transcript": "WorkSessionTranscript",
                "work-session-sources": "WorkSessionSources",
                "workplace-file": "WorkplaceFileContent",
            }[resource]
            checked = from_wire(
                ADAPTERS[expected],
                to_wire(custom_model, extensions=self.ports.extensions),
                extensions=self.ports.extensions,
            )
            if checked.scope.partition != scope.partition:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            if resource == "laboratory-runs":
                for item in checked.items:
                    self.sessions.check_scope(authority, item.lab_scope)
            if resource == "settings-transfer-status" and checked.record is not None:
                self.sessions.check_scope(authority, checked.record.source_scope)
            return _json(checked, self.ports.extensions)
        if resource == "sessions":
            run = self.supervisor.snapshot
            items = [run] if run.scope.partition == scope.partition else []
            if identifier is not None:
                model: Contract | None = run if items and run.id == identifier else None
            else:
                model = Page[RunSession](
                    items=items if cursor is None or run.id > cursor else [],
                    limit=limit,
                    next_cursor=None,
                    complete=True,
                )
        elif self.ports.reader is None:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
        else:
            if order is None:
                model = await self.ports.reader.read(
                    resource, authority.control.principal_id, scope, identifier, limit, cursor
                )
            else:
                model = await self.ports.reader.read(
                    resource,
                    authority.control.principal_id,
                    scope,
                    identifier,
                    limit,
                    cursor,
                    order=order,
                    utterance_id=utterance_id,
                )
        self.sessions.check_scope(authority, scope)  # logout/expiry while DB was awaited
        if model is None:
            raise Rejection(404, ErrorCode.ACCESS_DENIED)
        raw = to_wire(model, extensions=self.ports.extensions)
        expected = READ_MODELS[resource].__name__ + ("Page" if identifier is None else "")
        validated = from_wire(ADAPTERS[expected], raw, extensions=self.ports.extensions)
        rows = validated.items if identifier is None else [validated]
        if any(row.scope.partition != scope.partition for row in rows):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if identifier is None and (validated.limit != limit or len(rows) > limit):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        return Response(
            raw,
            media_type="application/json",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def service(
        self,
        path: str,
        method: str,
        headers: Mapping[str, str],
        receive: Receive,
    ) -> Response:
        if "origin" in headers or "cookie" in headers or "x-csrf-token" in headers:
            raise Rejection(403, ErrorCode.ACCESS_DENIED)
        authorization = headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise Rejection(401, ErrorCode.ACCESS_DENIED)
        try:
            authority = self.sessions.worker(authorization[7:])
        except ContractError:
            raise Rejection(401, ErrorCode.ACCESS_DENIED) from None
        match = re.fullmatch(r"/internal/v1/jobs/([^/]+)/(claim|lease|result)", path)
        if match is None or method != "POST":
            raise Rejection(404, ErrorCode.INVALID_ARGUMENTS)
        action = match.group(2)
        adapters = {
            "claim": "ServiceClaimCommand",
            "lease": "ServiceLeaseCommand",
            "result": "ServiceResultCommand",
        }
        request = from_wire(
            ADAPTERS[adapters[action]],
            await _body(headers, receive, MAX_BODY_BYTES),
            extensions=self.ports.extensions,
        )
        if request.target != match.group(1) or request.scope.partition not in authority.partitions:
            raise Rejection(403, ErrorCode.ACCESS_DENIED)
        if not authority.capability_ids:
            raise Rejection(403, ErrorCode.ACCESS_DENIED)
        jobs = self.ports.jobs
        if jobs is None:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
        if isinstance(request, ServiceClaimCommand):
            claim = await jobs.claim(authority, request)
            return _json(
                ServiceClaimReceipt(
                    request_id=request.request_id,
                    scope=request.scope,
                    command=None if claim is None else claim.command,
                    lease=None if claim is None else claim.lease,
                    acquired=False if claim is None else claim.acquired,
                ),
                self.ports.extensions,
            )
        if isinstance(request, ServiceLeaseCommand):
            if request.type == "job.renew":
                lease = await jobs.renew(authority, request)
                return _json(
                    ServiceLeaseReceipt(
                        request_id=request.request_id,
                        scope=request.scope,
                        lease=lease,
                    )
                )
            if request.type != "job.cancel_receipt":
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            job = await jobs.cancel_receipt(authority, request)
        elif isinstance(request, ServiceResultCommand):
            # Client cannot set transient/retry_safe or choose another attempt.
            job = await jobs.accept(authority, request)
        else:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        return _json(
            Receipt(
                request_id=request.request_id,
                scope=job.scope,
                status="completed",
                resource_id=job.id,
                operation_id=job.operation_id,
                version=job.version,
            )
        )

    async def websocket(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
        client: Subscriber | None = None
        reserved = False
        try:
            if self._websockets >= self.limits.websocket_clients:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            self._websockets += 1
            reserved = True
            if scope["path"] != "/api/v1/events":
                raise Rejection(404, ErrorCode.INVALID_ARGUMENTS)
            self.query(scope)  # never allow tokens, credentials, arbitrary query scopes in URLs
            headers = _headers(scope)
            authority = self.operator(headers, mutation=False)
            self.browser(headers, mutation=True)  # WS always requires explicit Origin
            if await receive() != {"type": "websocket.connect"}:
                raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
            # Handshake authorized before accept; scope is authorized before any event.
            await send({"type": "websocket.accept"})
            async with asyncio.timeout(2):
                message = await receive()
            if message["type"] != "websocket.receive" or message.get("bytes") is not None:
                raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
            raw = message.get("text", "").encode("utf-8")
            if len(raw) > 4096:
                raise Rejection(413, ErrorCode.INVALID_ARGUMENTS)
            subscription = from_wire(TypeAdapter(EventSubscription), raw)
            self.sessions.check_scope(authority, subscription.scope)
            client = self.events.subscribe(subscription)
            while True:
                self.sessions.check_scope(authority, subscription.scope)
                if client.closed:
                    raise ContractError(ErrorCode.RESOURCE_BUSY)
                while client.buffer:
                    self.sessions.check_scope(authority, subscription.scope)
                    async with asyncio.timeout(0.25):
                        await send(
                            {"type": "websocket.send", "text": client.buffer.popleft().decode()}
                        )
                try:
                    async with asyncio.timeout(0.1):
                        message = await receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    # Subscriptions cannot submit commands, streams, replays or change scope.
                    raise Rejection(403, ErrorCode.ACCESS_DENIED)
                except TimeoutError:
                    continue
        except ContractError as error:
            await send(
                {
                    "type": "websocket.close",
                    "code": 1013 if error.error.code == ErrorCode.RESOURCE_BUSY else 1008,
                }
            )
        except Rejection, TimeoutError, ValueError:
            await send({"type": "websocket.close", "code": 1008})
        finally:
            if reserved:
                self._websockets -= 1
            if client is not None:
                self.events.remove(client)


class AccessMiddleware:
    def __init__(self, app: ASGIApp, surface: Surface) -> None:
        self.app, self.surface = app, surface

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await self.surface.websocket(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if len(path) > 2048:
            await _error(413, ErrorCode.INVALID_ARGUMENTS)(scope, receive, send)
            return
        if path.startswith("/api/v1/opencode/"):
            from ai_agent_core.opencode_ui.sse import event_path, stream

            if event_path(path):
                await stream(self.surface, scope, receive, send)
                return
        if not path.startswith(("/api/", "/internal/")):
            try:
                self.surface.browser(
                    _headers(scope), mutation=scope["method"] not in ("GET", "HEAD")
                )
                if scope.get("query_string"):
                    raise Rejection(400, ErrorCode.INVALID_ARGUMENTS)
            except Rejection as error:
                await _error(error.status, error.code)(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        direct_stop = bool(re.fullmatch(r"/api/v1/agents/[^/]+/stop", path))
        if direct_stop and self.surface._stops >= self.surface.limits.sessions:
            await _error(429, ErrorCode.RESOURCE_BUSY)(scope, receive, send)
            return
        if direct_stop:
            self.surface._stops += 1
        counted = not direct_stop
        if counted and self.surface._requests >= self.surface.limits.requests:
            await _error(429, ErrorCode.RESOURCE_BUSY)(scope, receive, send)
            return
        if counted:
            self.surface._requests += 1
        try:
            async with asyncio.timeout(10):
                response = await self.surface.http(scope, receive)
        except Rejection as error:
            response = _error(error.status, error.code)
        except ContractError as error:
            response = _error(STATUS.get(error.error.code, 400), error.error.code)
        except TimeoutError:
            response = _error(503, ErrorCode.SERVICE_UNAVAILABLE)
        except Exception:
            # Provider/SQL/Pydantic/raw input errors never enter network responses or logs.
            response = _error(503, ErrorCode.SERVICE_UNAVAILABLE)
        finally:
            if counted:
                self.surface._requests -= 1
            if direct_stop:
                self.surface._stops -= 1
        await response(scope, receive, send)


def install_access(
    app: FastAPI,
    supervisor: Supervisor,
    settings: BootstrapSettings,
    ports: AccessPorts | None = None,
) -> Surface:
    surface = Surface(supervisor, settings, ports or AccessPorts())
    app.add_middleware(AccessMiddleware, surface=surface)
    return surface
