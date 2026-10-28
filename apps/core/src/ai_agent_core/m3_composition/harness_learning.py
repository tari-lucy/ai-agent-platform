"""Doc33: import completed harness text through the memory owner, never through old jobs.

The checkpoint contains identities and digests only. It is a cursor, not another memory
store. Losing it is safe: external Source identities and episode operation ids are stable,
and the independent deletion ledger is checked before importing anything again.
No model call, conversation rewrite, rights grant or runtime startup occurs here.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import quote

from ai_agent_application.memory import EpisodeDraft, MemoryService, SourceDraft, derived_id
from ai_agent_application.retention.ports import DeletionLedger
from ai_agent_application.storage import Owner
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.states import ErrorCode, Provenance
from pydantic import BaseModel, ConfigDict, Field

from .harness_memory import SOURCE_TYPES
from .state import command

if TYPE_CHECKING:
    from .service import Runtime

ADAPTER = "opencode-harness"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
LOG = logging.getLogger(__name__)


class OpenCode(Protocol):
    async def opencode(
        self, method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float = 60
    ) -> Any: ...


class Imported(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str
    message: str
    role: Literal["user", "assistant"]
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: SourceRef
    episode_done: bool = False


class SessionNotice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Literal["source_unavailable", "message_changed"]
    sources: list[SourceRef] = Field(default_factory=list, max_length=200)
    complete: bool = True


class Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    partition: tuple[str, str, str, str]
    messages: dict[str, Imported] = Field(default_factory=dict, max_length=10000)
    skipped: dict[str, Literal["text_limit"]] = Field(default_factory=dict, max_length=10000)
    # A sticky warning: a pre-existing harness history may still contain removed text.
    notices: dict[str, SessionNotice] = Field(default_factory=dict, max_length=200)

    def mark(
        self,
        session: str,
        reason: Literal["source_unavailable", "message_changed"],
        source: SourceRef | None = None,
    ) -> None:
        notice = self.notices.setdefault(session, SessionNotice(reason=reason))
        if source is not None and source not in notice.sources:
            if len(notice.sources) < 200:
                notice.sources.append(source)
            else:
                notice.complete = False


def atomic_json(path: Path, value: BaseModel) -> None:
    """Private, bounded metadata, replaced only after a complete write (one host writer)."""
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("HARNESS_CHECKPOINT_PATH")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".harness-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            encoded = value.model_dump_json()
            if len(encoded.encode("utf-8")) > 8 * 1024 * 1024:
                raise ValueError("HARNESS_CHECKPOINT_LIMIT")
            type(value).model_validate_json(encoded)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(4):
            try:
                os.replace(name, path)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.02)  # short-lived Windows readers/scanners; never unlink the cursor
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def identifier(value: Any) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise ValueError("HARNESS_RESPONSE_ID")
    return value


class HarnessLearning:
    """Explicit tick/close port; the harness lifecycle owner schedules and cancels ticks.

    authorize must check the captured ACTIVE epoch and exact memory partition, also at
    commit through the store fence. api_transfer is an explicit host policy, never inferred
    from a message. A session listing must belong to this persona's own HarnessHost.
    """

    def __init__(
        self,
        host: OpenCode,
        memory: MemoryService,
        ledger: DeletionLedger,
        checkpoint: Path,
        authorize: Callable[[Scope], None],
        *,
        api_transfer: bool = False,
    ) -> None:
        self.host, self.memory, self.ledger = host, memory, ledger
        self.path, self.authorize, self.api_transfer = checkpoint, authorize, api_transfer
        self.lock = asyncio.Lock()
        self.closed = False

    def _load(self, scope: Scope) -> Cursor:
        if not self.path.is_absolute() or self.path.is_symlink():
            raise ValueError("HARNESS_CHECKPOINT_PATH")
        if not self.path.exists():
            return Cursor(partition=scope.partition)
        if self.path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("HARNESS_CHECKPOINT_LIMIT")
        cursor = Cursor.model_validate_json(self.path.read_bytes())
        if cursor.partition != scope.partition:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        return cursor

    def _current(self, scope: Scope) -> None:
        if self.closed:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        self.authorize(scope)

    async def notices(self, scope: Scope) -> dict[str, SessionNotice]:
        """Metadata for a panel projection; never returns harness message text."""
        cursor = self._load(scope)
        await self._audit(scope, cursor)
        return dict(cursor.notices)

    async def _audit(self, scope: Scope, cursor: Cursor) -> set[str]:
        blocked = {entry.source_id for entry in await self.ledger.entries(scope)}
        for imported in cursor.messages.values():
            source = await self.memory.get_source(scope, imported.source.source_id)
            if (
                imported.source.scope.partition != scope.partition
                or source is None
                or source.version != imported.source.version
                or source.id in blocked
                or source.availability != "available"
                or not source.read_allowed
                or (
                    source.retention.expires_at is not None
                    and source.retention.expires_at <= self.memory.clock()
                )
                or (self.api_transfer and not source.api_transfer_allowed)
            ):
                cursor.mark(imported.session, "source_unavailable", imported.source)
        return blocked

    async def tick(self, scope: Scope) -> int:
        async with self.lock:
            self._current(scope)
            cursor = self._load(scope)
            blocked = await self._audit(scope, cursor)
            self._current(scope)
            atomic_json(self.path, cursor)
            rows = await self.host.opencode("GET", "/session")
            self._current(scope)
            if not isinstance(rows, list) or len(rows) > 200:
                raise ValueError("HARNESS_SESSION_LIMIT")
            added = 0
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("HARNESS_SESSION_SHAPE")
                session = identifier(row.get("id"))
                if session in cursor.notices:
                    continue
                messages = await self.host.opencode("GET", f"/session/{quote(session)}/message")
                self._current(scope)
                if not isinstance(messages, list) or len(messages) > 2000:
                    raise ValueError("HARNESS_MESSAGE_LIMIT")
                for message in sorted(messages, key=_order):
                    added += await self._import(scope, session, message, cursor, blocked)
            return added

    async def _import(
        self, scope: Scope, session: str, row: Any, cursor: Cursor, blocked: set[str]
    ) -> int:
        if not isinstance(row, dict) or not isinstance(row.get("info"), dict):
            raise ValueError("HARNESS_MESSAGE_SHAPE")
        info = row["info"]
        if info.get("sessionID") != session:
            raise ValueError("HARNESS_SESSION_MISMATCH")
        role = info.get("role")
        if role not in ("user", "assistant"):
            return 0
        times = info.get("time", {})
        if role == "assistant" and (
            not times.get("completed") or info.get("error") or info.get("finish") == "tool-calls"
        ):
            return 0  # never turn a draft, reasoning or a failed turn into confirmed experience
        mid = identifier(info.get("id"))
        key = derived_id("harness-message", scope, session, mid)
        if key in cursor.skipped:
            return 0
        parts = row.get("parts")
        if not isinstance(parts, list):
            raise ValueError("HARNESS_PARTS_SHAPE")
        # Tools, files, reasoning and synthetic context are not operator speech or rights.
        text = "\n".join(
            p["text"]
            for p in parts
            if isinstance(p, dict)
            and p.get("type") == "text"
            and isinstance(p.get("text"), str)
            and not p.get("synthetic")
            and not p.get("ignored")
        )
        if not text.strip():
            return 0
        if len(text) > 16000:
            # Explicit incomplete outcome, no truncation; unrelated fresh messages still flow.
            if len(cursor.skipped) >= 10000:
                raise ValueError("HARNESS_CHECKPOINT_LIMIT")
            cursor.skipped[key] = "text_limit"
            atomic_json(self.path, cursor)
            LOG.warning("Harness learning deferred: HARNESS_TEXT_LIMIT")
            return 0
        stamp = times.get("created")
        if type(stamp) not in (int, float):
            raise ValueError("HARNESS_TIME_SHAPE")
        occurred = datetime.fromtimestamp(stamp / 1000, UTC)
        digest = hashlib.sha256(json.dumps([role, text, stamp]).encode()).hexdigest()
        previous = cursor.messages.get(key)
        added = previous is None
        if previous is not None and previous.digest != digest:
            cursor.mark(session, "message_changed", previous.source)
            atomic_json(self.path, cursor)
            return 0
        if session in cursor.notices:
            return 0
        source_type = "harness-operator-message" if role == "user" else "harness-agent-message"
        assert source_type in SOURCE_TYPES
        source_id = derived_id("src", scope, "external", source_type, ADAPTER, key)
        if source_id in blocked:
            cursor.mark(
                session,
                "source_unavailable",
                SourceRef(source_id=source_id, version=1, scope=scope, availability="unavailable"),
            )
            atomic_json(self.path, cursor)
            return 0
        if occurred + self.memory.ttl <= self.memory.clock():
            # Do not refresh retention from the import time or starve independent sessions.
            cursor.mark(session, "source_unavailable")
            atomic_json(self.path, cursor)
            return 0
        self._current(scope)
        if previous is None:
            if len(cursor.messages) >= 10000:
                raise ValueError("HARNESS_CHECKPOINT_LIMIT")
            provenance = (
                Provenance.SIMULATION
                if scope.memory_mode == "test"
                else Provenance.MODEL_PROPOSAL
                if role == "assistant"
                else Provenance.THIRD_PARTY_MESSAGE
            )
            with self.memory.storage.fence(lambda: self._current(scope), owner=Owner.MEMORY):
                made = await self.memory.register_source(
                    command(scope, key),
                    SourceDraft(
                        provenance=provenance,
                        source_type=source_type,
                        adapter_id=ADAPTER,
                        adapter_version="1",
                        occurred_at=occurred,
                        content=text,
                        read_allowed=True,
                        api_transfer_allowed=self.api_transfer,
                        public_disclosure_allowed=False,
                        external_id=key,
                        author_id="harness-" + role,
                    ),
                )
            previous = Imported(
                session=session,
                message=mid,
                role=role,
                digest=digest,
                source=SourceRef(
                    source_id=made.source_id,
                    version=made.version,
                    scope=scope,
                    availability="available",
                ),
            )
            cursor.messages[key] = previous
            atomic_json(self.path, cursor)
        if role == "assistant" and not previous.episode_done:
            parent = info.get("parentID")
            user = cursor.messages.get(derived_id("harness-message", scope, session, str(parent)))
            if user is not None and user.role == "user":
                refs = (user.source, previous.source)
                blocked = {entry.source_id for entry in await self.ledger.entries(scope)}
                for ref in refs:
                    source = await self.memory.get_source(scope, ref.source_id)
                    if (
                        source is None
                        or source.version != ref.version
                        or source.id in blocked
                        or not source.read_allowed
                        or source.availability != "available"
                        or (self.api_transfer and not source.api_transfer_allowed)
                    ):
                        cursor.mark(session, "source_unavailable", ref)
                        atomic_json(self.path, cursor)
                        return int(added)
                with self.memory.storage.fence(lambda: self._current(scope), owner=Owner.MEMORY):
                    await self.memory.record_episode(
                        command(scope, "episode-" + key),
                        EpisodeDraft(
                            sources=refs,
                            experience_source_ids=tuple(r.source_id for r in refs),
                            participant_ids=(),
                            choice="Участие в разговоре OpenCode.",
                            actual_outcome="Ответ сохранён харнесом; получение не подтверждено.",
                            interpretation="Слова модели не доказывают выполнение действий.",
                            operation_id=key,
                            provenance="simulation"
                            if scope.memory_mode == "test"
                            else "observation",
                        ),
                    )
                previous.episode_done = True
                atomic_json(self.path, cursor)
        return int(added)

    async def close(self) -> None:
        self.closed = True
        async with self.lock:
            pass


def _order(row: Any) -> tuple[float, str]:
    if not isinstance(row, dict) or not isinstance(row.get("info"), dict):
        raise ValueError("HARNESS_MESSAGE_SHAPE")
    info = row["info"]
    stamp = info.get("time", {}).get("created")
    if type(stamp) not in (float, int):
        raise ValueError("HARNESS_TIME_SHAPE")
    return float(stamp), identifier(info.get("id"))


class LearningPart:
    """HarnessPart lifecycle: no work between ticks; restart creates a fresh epoch guard."""

    def __init__(self, runtime: Runtime, directory: Path) -> None:
        self.rt, self.directory = runtime, directory
        self.task: asyncio.Task[int] | None = None
        self.service: HarnessLearning | None = None
        self.next_check = 0.0

    def path(self, scope: Scope) -> Path:
        return self.directory / (derived_id("learning", scope) + ".json")

    async def notices(self, scope: Scope) -> dict[str, SessionNotice]:
        path = self.path(scope)
        if not path.exists():
            return {}
        ledger, host = getattr(self.rt.maintenance, "ledger", None), self.rt.harness
        if ledger is None or host is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        from .state import PRINCIPALS

        owner = MemoryService(
            self.rt.store,
            PRINCIPALS[Owner.MEMORY],
            self.rt.registry,
            clock=self.rt.supervisor.clock.utc,
        )
        budget = self.rt.private_api_budget
        view = HarnessLearning(
            host,
            owner,
            ledger,
            path,
            lambda _: None,
            api_transfer=budget is not None and budget.memory_processing,
        )
        return await view.notices(scope)  # read-only; works while STOPPED, no OpenCode API needed

    def tick(self) -> None:
        host = self.rt.harness
        if host is None or not host.current():
            return
        if self.task is not None:
            if not self.task.done():
                return
            try:
                self.task.result()
            except (ContractError, OSError, ValueError, TimeoutError) as error:
                # No payload, exception detail, message text or provider URL in this log.
                LOG.warning("Harness learning deferred: %s", type(error).__name__)
            self.task = None
        if time.monotonic() < self.next_check:
            return
        self.next_check = time.monotonic() + 15
        ledger = getattr(self.rt.maintenance, "ledger", None)
        if ledger is None:
            LOG.warning("Harness learning deferred: LEDGER_UNAVAILABLE")
            return
        run = self.rt.supervisor.snapshot
        epoch, scope = run.control_epoch, run.scope

        def guard(stable: Scope) -> None:
            now = self.rt.supervisor.snapshot
            if (
                not host.current()
                or now.control_epoch != epoch
                or stable.partition != scope.partition
                or now.scope.partition != scope.partition
            ):
                raise ContractError(ErrorCode.STALE_CONTEXT)

        from .state import PRINCIPALS

        owner = MemoryService(
            self.rt.store,
            PRINCIPALS[Owner.MEMORY],
            self.rt.registry,
            index=self.rt.dialogue_index,
            clock=self.rt.supervisor.clock.utc,
            reader_access=self.rt.readers,
        )
        # Existing explicit memory-processing approval; the conversation cannot grant transfer.
        budget = self.rt.private_api_budget
        self.service = HarnessLearning(
            host,
            owner,
            ledger,
            self.path(scope),
            guard,
            api_transfer=budget is not None and budget.memory_processing,
        )
        self.task = asyncio.create_task(self.service.tick(scope))

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.service is not None:
            await self.service.close()
            self.service = None
        self.next_check = 0.0
