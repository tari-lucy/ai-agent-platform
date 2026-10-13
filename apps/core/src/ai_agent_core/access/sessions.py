"""Bounded process-local credentials. Restart discards every browser session."""

import asyncio
import hashlib
import hmac
import re
import secrets
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta

from ai_agent_application.control.ports import Clock, ControlAuthority
from ai_agent_application.jobs.ports import WorkerAuthority
from ai_agent_contracts.access import OperatorSession
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode
from pydantic import SecretStr

COOKIE = "ai_operator_session"
TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
HASH = re.compile(r"^scrypt-v1\$([a-f0-9]{32})\$([a-f0-9]{64})$")
N, R, P = 131072, 8, 1  # one bounded password worker, about 128 MiB working memory


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=N, r=R, p=P, dklen=32, maxmem=192 * 1024 * 1024
    )


def password_hash(password: SecretStr) -> SecretStr:
    """Local setup utility: caller must store privately, never print it in a report."""
    value = password.get_secret_value()
    if not 1 <= len(value.encode("utf-8")) <= 1024:
        raise ValueError("PASSWORD_LENGTH")
    salt = secrets.token_bytes(16)
    return SecretStr("scrypt-v1$" + salt.hex() + "$" + _derive(value, salt).hex())


@dataclass(frozen=True)
class AccessLimits:
    sessions: int = 8
    login_attempts: int = 5
    login_window: int = 60
    session_seconds: int = 3600
    idle_seconds: int = 900
    requests: int = 64
    websocket_clients: int = 32
    websocket_events: int = 256

    def __post_init__(self) -> None:
        maxima = (8, 10, 300, 86400, 3600, 64, 32, 256)
        values = (
            self.sessions,
            self.login_attempts,
            self.login_window,
            self.session_seconds,
            self.idle_seconds,
            self.requests,
            self.websocket_clients,
            self.websocket_events,
        )
        if any(
            type(value) is not int or not 1 <= value <= maximum
            for value, maximum in zip(values, maxima, strict=True)
        ):
            raise ValueError("ACCESS_LIMIT")
        if self.idle_seconds > self.session_seconds:
            raise ValueError("ACCESS_IDLE_LIMIT")


@dataclass(frozen=True)
class WorkerGrant:
    """Trusted core composition, never a caller-selected scope/capability list."""

    id: str
    partitions: frozenset[tuple[str, str, str, str]]
    capability_ids: frozenset[str]

    def __post_init__(self) -> None:
        from ai_agent_contracts.common import Id
        from pydantic import TypeAdapter

        TypeAdapter(Id).validate_python(self.id)
        if not 1 <= len(self.partitions) <= 8 or len(self.capability_ids) > 64:
            raise ValueError("WORKER_GRANT")
        for partition in self.partitions:
            Scope.model_validate(
                dict(
                    zip(
                        ("workspace_id", "persona_id", "memory_space_id", "memory_mode"),
                        partition,
                        strict=True,
                    )
                )
            )
        for capability in self.capability_ids:
            TypeAdapter(Id).validate_python(capability)


@dataclass(repr=False)
class _Session:
    issued: float
    touched: float
    expires: float


@dataclass(frozen=True, repr=False)
class SessionAuthority:
    """Unforgeable by HTTP; checked against live RAM session after every awaited read."""

    key: bytes = field(repr=False)
    control: ControlAuthority


class Sessions:
    def __init__(
        self,
        scopes: tuple[Scope, ...],
        *,
        clock: Clock,
        password: SecretStr | None = None,
        service_token: SecretStr | None = None,
        worker: WorkerGrant | None = None,
        limits: AccessLimits | None = None,
    ) -> None:
        if not 1 <= len(scopes) <= 8:
            raise ValueError("OPERATOR_SCOPES")
        self.scopes = tuple(Scope.model_validate(scope.model_dump()) for scope in scopes)
        if any(
            s.agent_id is not None or s.run_id is not None or s.session_id is not None
            for s in self.scopes
        ):
            raise ValueError("OPERATOR_PARTITIONS_ONLY")
        self.clock, self.limits = clock, limits or AccessLimits()
        self._password = password
        self._service_token = service_token
        self._worker = worker
        self._sessions: dict[bytes, _Session] = {}
        self._attempts: deque[float] = deque(maxlen=self.limits.login_attempts)
        self._login_task: asyncio.Task[bool] | None = None
        self._master = secrets.token_bytes(32)
        self._closed = False
        self._last = clock.elapsed()
        if password is not None and HASH.fullmatch(password.get_secret_value()) is None:
            raise ValueError("PASSWORD_HASH")
        if (
            service_token is not None
            and re.fullmatch(r"[a-f0-9]{64}", service_token.get_secret_value()) is None
        ):
            raise ValueError("WORKER_TOKEN")

    def _now(self) -> float:
        now = self.clock.elapsed()
        if now < self._last:
            self._sessions.clear()
            self._attempts.clear()
        self._last = now
        for key, session in list(self._sessions.items()):
            if now >= session.expires or now - session.touched >= self.limits.idle_seconds:
                del self._sessions[key]
        return now

    def _verify_password(self, password: SecretStr) -> bool:
        assert self._password is not None
        match = HASH.fullmatch(self._password.get_secret_value())
        assert match is not None
        salt, expected = match.groups()
        try:
            actual = _derive(password.get_secret_value(), bytes.fromhex(salt))
            return hmac.compare_digest(actual, bytes.fromhex(expected))
        except ValueError:
            return False

    async def login(self, password: SecretStr) -> tuple[str, SessionAuthority]:
        if self._closed or self._password is None:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
        now = self._now()
        while self._attempts and now - self._attempts[0] >= self.limits.login_window:
            self._attempts.popleft()
        if len(self._attempts) >= self.limits.login_attempts or (
            self._login_task is not None and not self._login_task.done()
        ):
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        if len(self._sessions) >= self.limits.sessions:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        self._attempts.append(now)
        # Shield preserves the one-worker bound if a disconnected HTTP caller is cancelled.
        self._login_task = asyncio.create_task(asyncio.to_thread(self._verify_password, password))
        valid = await asyncio.shield(self._login_task)
        if self._closed:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
        if not valid:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        now = self._now()
        if len(self._sessions) >= self.limits.sessions:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        cookie = secrets.token_urlsafe(32)
        key = hashlib.sha256(cookie.encode("ascii")).digest()
        self._sessions[key] = _Session(now, now, now + self.limits.session_seconds)
        return cookie, self.authorize(cookie)

    def authorize(self, cookie: str, *, touch: bool = True) -> SessionAuthority:
        if self._closed or TOKEN.fullmatch(cookie) is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        now = self._now()
        key = hashlib.sha256(cookie.encode("ascii")).digest()
        session = self._sessions.get(key)
        if session is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if touch:
            session.touched = now
        return SessionAuthority(
            key,
            ControlAuthority(
                "operator",
                frozenset(scope.partition for scope in self.scopes),
                session.expires,
                is_current=lambda: self._valid(key),
            ),
        )

    def _valid(self, key: bytes) -> bool:
        self._now()
        return not self._closed and key in self._sessions

    def shutdown(self) -> None:
        self._closed = True
        self._sessions.clear()

    def current(self, authority: SessionAuthority) -> None:
        self._now()
        session = self._sessions.get(authority.key)
        if session is None or session.expires != authority.control.expires_at:
            raise ContractError(ErrorCode.ACCESS_DENIED)

    def csrf(self, authority: SessionAuthority, token: str) -> None:
        self.current(authority)
        if TOKEN.fullmatch(token) is None or not hmac.compare_digest(
            token, self._csrf(authority.key)
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)

    def _csrf(self, key: bytes) -> str:
        import base64

        return (
            base64.urlsafe_b64encode(hmac.digest(self._master, key, "sha256")).decode().rstrip("=")
        )

    def info(self, authority: SessionAuthority) -> OperatorSession:
        self.current(authority)
        remaining = authority.control.expires_at - self.clock.elapsed()
        return OperatorSession(
            principal_id=authority.control.principal_id,
            scopes=list(self.scopes),
            expires_at=self.clock.utc() + timedelta(seconds=remaining),
            csrf_token=self._csrf(authority.key),
        )

    def check_scope(self, authority: SessionAuthority, scope: Scope) -> None:
        self.current(authority)
        if scope.partition not in authority.control.partitions:
            raise ContractError(ErrorCode.ACCESS_DENIED)

    def logout(self, authority: SessionAuthority) -> None:
        self.current(authority)
        del self._sessions[authority.key]

    def worker(self, bearer: str) -> WorkerAuthority:
        if self._closed or self._service_token is None or self._worker is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if not re.fullmatch(r"[a-f0-9]{64}", bearer) or not hmac.compare_digest(
            bearer, self._service_token.get_secret_value()
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        return WorkerAuthority(
            self._worker.id,
            self._worker.partitions,
            self._worker.capability_ids,
            self.clock.elapsed() + 20,
        )
