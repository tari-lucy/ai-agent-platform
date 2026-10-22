"""Skill library owner (AW-03): versioned methods over existing capabilities.

A skill never adds a tool, right, schedule or budget: ``uses_capabilities`` only describes the
method, execution is still checked against current permissions. The method text and helper
scripts are managed files of the catalogue owner (WorkspaceFiles); without a runner (AW-04) a
script is only a file. ``verified`` needs evidence of a confirmed result, not confidence.
"""

import builtins
import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from ai_agent_contracts.agent_workplace import FileWrite, ManagedFile, SkillVersion
from ai_agent_contracts.api import MessageArguments, MessageCommand, Receipt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import ArtifactRef, Scope, SourceRef
from ai_agent_contracts.states import ErrorCode

from ai_agent_application.agent_workplace.ports import WorkAdmission, WorkspaceFiles
from ai_agent_application.storage import (
    Owner,
    Principal,
    RecordAccess,
    RecordRepository,
    Storage,
)

MAX_SCAN = 2000
WORD = re.compile(r"\w{3,}", re.UNICODE)


@dataclass(frozen=True)
class SkillDraft:
    skill_id: str
    title: str
    applicability: str
    limitations: str
    change_reason: str
    uses_capabilities: tuple[str, ...] = ()
    helper_files: tuple[ArtifactRef, ...] = field(default_factory=tuple)


def area(scope: Scope) -> Scope:
    return Scope(
        workspace_id=scope.workspace_id,
        persona_id=scope.persona_id,
        memory_space_id=scope.memory_space_id,
        memory_mode=scope.memory_mode,
    )


def method_file_id(skill_id: str) -> str:
    return "skill-method." + skill_id


def _slug(skill_id: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", skill_id.lower()).strip("-_") or "skill"
    return slug[:64]


def ref(file: ManagedFile) -> ArtifactRef:
    return file.reference


def terms(text: str) -> set[str]:
    return {word.casefold() for word in WORD.findall(text)}


class SkillLibrary:
    def __init__(
        self,
        store: Storage,
        files: WorkspaceFiles,
        clock: Callable[[], datetime],
        readers: tuple[RecordAccess, ...] = (),
        principal: Principal | None = None,
    ) -> None:
        self.store, self.files, self.clock, self.readers = store, files, clock, readers
        self.principal = principal or Principal("core-skills", "core_owner", Owner.SKILLS)

    def _repo(self, scope: Scope) -> RecordRepository[SkillVersion]:
        return self.store.repository(SkillVersion, self.principal, area(scope))

    async def get(
        self, scope: Scope, skill_id: str, version: int | None = None
    ) -> SkillVersion | None:
        return await self._repo(scope).get(skill_id, version=version)

    async def all(self, scope: Scope) -> builtins.list[SkillVersion]:
        rows: builtins.list[SkillVersion] = []
        cursor: str | None = None
        while len(rows) < MAX_SCAN:
            page = await self._repo(scope).list(limit=200, after_id=cursor)
            rows += page
            if len(page) < 200:
                break
            cursor = page[-1].id
        return rows

    async def find(
        self, scope: Scope, query: str, *, limit: int = 10, include_retired: bool = False
    ) -> builtins.list[SkillVersion]:
        """Plain term overlap over title/applicability/limitations; ties by recency."""
        wanted = terms(query)
        scored: builtins.list[tuple[int, SkillVersion]] = []
        for row in await self.all(scope):
            if row.status == "retired" and not include_retired:
                continue
            score = len(wanted & terms(" ".join((row.title, row.applicability, row.limitations))))
            if score or not wanted:
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], -item[1].updated_at.timestamp(), item[1].id))
        return [row for _, row in scored[:limit]]

    async def read(self, admission: WorkAdmission, skill_id: str) -> tuple[SkillVersion, bytes]:
        """The method through the file owner: its sources, TTL and rights are checked there."""
        skill = await self.get(admission.scope, skill_id)
        if skill is None or skill.status == "retired":
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        _, body = await self.files.read(admission, skill.method.artifact_id, skill.method.version)
        return skill, body

    async def _commit(self, request_id: str, skill: SkillVersion, expected: int | None) -> Receipt:
        payload = json.dumps(skill.model_dump(mode="json", exclude={"created_at", "updated_at"}))
        command = MessageCommand(
            request_id=request_id,
            scope=skill.scope,
            target=skill.id,
            type="dialogue.message",
            arguments=MessageArguments(text=hashlib.sha256(payload.encode()).hexdigest()),
        )
        async with self.store.transaction(self.principal, skill.scope, command) as unit:
            if unit.replay is not None:
                return unit.replay
            await unit.put(skill, expected_version=expected, access=self.readers or None)
            receipt = Receipt(
                request_id=request_id,
                scope=skill.scope,
                status="completed",
                resource_id=skill.id,
                version=skill.version,
            )
            await unit.complete(receipt)
            return receipt

    async def save(
        self,
        admission: WorkAdmission,
        request_id: str,
        draft: SkillDraft,
        method: bytes,
        expected_version: int | None,
    ) -> SkillVersion:
        """Create or improve a skill. A changed method is experimental again until verified.

        The new revision is built from the pinned ``expected_version`` (not from whatever is
        current), so a retry of the same request is byte-identical and replays at both owners
        (file and skill); a different request on an outdated version is a VERSION_CONFLICT.
        """
        scope = area(admission.scope)
        digest = hashlib.sha256(method).hexdigest()
        current = await self.get(scope, draft.skill_id)
        if (current.version if current else None) != expected_version:
            # Moved on: only a replay of this very change may continue (its revision is
            # already the next one). Anything else is refused before any file is written.
            written = await self.get(scope, draft.skill_id, version=(expected_version or 0) + 1)
            if written is None or (
                written.title,
                written.applicability,
                written.limitations,
                written.change_reason,
                written.method.checksum,
            ) != (draft.title, draft.applicability, draft.limitations, draft.change_reason, digest):
                raise ContractError(
                    ErrorCode.VERSION_CONFLICT, request_id, current.version if current else None
                )
        pinned = None
        if expected_version is not None:
            pinned = await self.get(scope, draft.skill_id, version=expected_version)
            if pinned is None:
                raise ContractError(ErrorCode.VERSION_CONFLICT, request_id)
        file = await self.files.write(
            admission,
            FileWrite(
                request_id=request_id + ".method",
                file_id=method_file_id(draft.skill_id),
                expected_version=pinned.method.version if pinned else None,
                area="skills",
                path=_slug(draft.skill_id) + "/method.md",
                title=draft.title,
                kind="method",
                provenance="generated",
                media_type="text/markdown",
                checksum=digest,
                size_bytes=len(method),
                attempt_id=admission.attempt_id,
                change_reason=draft.change_reason[:512],
            ),
            method,
        )
        now = self.clock()
        skill = SkillVersion(
            id=draft.skill_id,
            scope=scope,
            created_at=pinned.created_at if pinned else now,
            updated_at=now,
            version=(pinned.version + 1) if pinned else 1,
            skill_id=draft.skill_id,
            title=draft.title,
            applicability=draft.applicability,
            limitations=draft.limitations,
            method=ref(file),
            helper_files=list(draft.helper_files),
            uses_capabilities=list(draft.uses_capabilities),
            status="experimental",
            evidence=[],
            change_reason=draft.change_reason,
        )
        await self._commit(request_id, skill, expected_version)
        stored = await self.get(scope, draft.skill_id, version=skill.version)
        assert stored is not None
        return stored

    async def verify(
        self,
        admission: WorkAdmission,
        request_id: str,
        skill_id: str,
        expected_version: int,
        evidence: Sequence[SourceRef],
        reason: str,
    ) -> SkillVersion:
        """Mark the current method verified by confirmed results; storage checks the sources."""
        if not evidence:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, request_id)
        return await self._status(
            admission, request_id, skill_id, expected_version, "verified", evidence, reason
        )

    async def retire(
        self, admission: WorkAdmission, request_id: str, skill_id: str, expected: int, reason: str
    ) -> SkillVersion:
        return await self._status(admission, request_id, skill_id, expected, "retired", (), reason)

    async def _status(
        self,
        admission: WorkAdmission,
        request_id: str,
        skill_id: str,
        expected: int,
        status: str,
        evidence: Sequence[SourceRef],
        reason: str,
    ) -> SkillVersion:
        scope = area(admission.scope)
        current = await self.get(scope, skill_id)
        if current is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, request_id)
        if current.version != expected:
            raise ContractError(ErrorCode.VERSION_CONFLICT, request_id, current.version)
        data = current.model_dump()
        data.update(
            version=current.version + 1,
            updated_at=self.clock(),
            status=status,
            evidence=[*current.evidence, *evidence][-200:] if status == "verified" else [],
            change_reason=reason,
        )
        await self._commit(request_id, SkillVersion.model_validate(data), expected)
        stored = await self.get(scope, skill_id)
        assert stored is not None
        return stored
