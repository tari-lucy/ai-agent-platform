"""Workplace files, skills and knowledge (AW-03) composed over the AW-02 file owner.

Only a host prepared by the offline ``enable-workplace-files`` builds these services. The file
owner asks ``authorize`` before and after every IO: it passes only with a current automatic
Permission of the workplace agent for the capability of the attempt doing the IO, and only for
the areas named in that permission. Permissions come from the operator's structured decision
(AW-01); a skill or note text never adds one. Sources and file versions are checked by the
release guard against the host deletion ledger. Nothing here starts work or calls a model.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ai_agent_adapters.workspace_files.directory import DirectoryBlobs
from ai_agent_adapters.workspace_files.usage import CatalogueUsage
from ai_agent_application.agent_knowledge import KnowledgeBase
from ai_agent_application.agent_skills import SkillLibrary
from ai_agent_application.agent_workplace.ports import WorkAdmission
from ai_agent_application.retention.ports import DeletionLedger
from ai_agent_application.storage import Owner
from ai_agent_application.workspace_files import FileLimits, FileReleaseGuard, ManagedFiles
from ai_agent_application.workspace_files.guard import stable_scope
from ai_agent_application.workspace_files.service import FileAuthorizer
from ai_agent_contracts.agent_workplace import FileAccess, WorkAttempt, WorkplaceLibrary
from ai_agent_contracts.api import Permission
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode

from .permission_decisions import HOLDER
from .state import PRINCIPALS

if TYPE_CHECKING:
    from .service import Runtime

CONDITIONS = "workplace.permission-conditions"
LIBRARY_AREAS = ("skills", "knowledge", "inbox", "reference")
HIDDEN = {ErrorCode.ACCESS_DENIED, ErrorCode.EXPIRED, ErrorCode.STALE_CONTEXT}


@dataclass(frozen=True)
class Workplace:
    files: ManagedFiles
    skills: SkillLibrary
    knowledge: KnowledgeBase


class Attempts:
    """AttemptContext: the activity owner's attempt record, read by the catalogue owner."""

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt

    async def attempt(self, scope: Scope, attempt_id: str) -> WorkAttempt | None:
        return await self.rt.store.repository(WorkAttempt, PRINCIPALS[Owner.CATALOGUE], scope).get(
            attempt_id
        )


async def granted_areas(rt: Runtime, scope: Scope, capability_id: str) -> set[str]:
    """Areas of the agent's current automatic right for one capability; empty if none."""
    now = rt.supervisor.clock.utc()
    rights = await rt.store.repository(
        Permission, PRINCIPALS[Owner.WORKSPACE], stable_scope(scope)
    ).list(limit=200)
    areas: set[str] = set()
    for right in rights:
        data = right.conditions.data
        named = data.get("areas") if isinstance(data, dict) else None
        if (
            right.principal_id == HOLDER
            and right.capability_id == capability_id
            and right.level == "automatic"
            and right.revoked_at is None
            and right.expires_at > now
            and right.conditions.schema_id == CONDITIONS
            and isinstance(named, list)
        ):
            areas.update(str(area) for area in named)
    return areas


def authorizer(rt: Runtime, attempts: Attempts) -> FileAuthorizer:
    async def authorize(
        admission: WorkAdmission, operation: Literal["read", "write"], area: str
    ) -> None:
        attempt = await attempts.attempt(admission.scope, admission.attempt_id)
        if attempt is None or attempt.operation_id != admission.operation_id:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if area not in await granted_areas(rt, admission.scope, attempt.capability_id):
            raise ContractError(ErrorCode.ACCESS_DENIED)

    return authorize


def build(rt: Runtime, ledger: DeletionLedger, blob_dir: Path, limits: FileLimits) -> Workplace:
    clock = rt.supervisor.clock.utc
    catalogue = PRINCIPALS[Owner.CATALOGUE]
    attempts = Attempts(rt)
    files = ManagedFiles(
        rt.store,
        catalogue,
        DirectoryBlobs(blob_dir),
        FileReleaseGuard(rt.store, catalogue, ledger, clock=clock),
        attempts,
        CatalogueUsage(rt.store).counts,
        limits,
        authorize=authorizer(rt, attempts),
        clock=clock,
        # The person's own files are read back into its own model (api_transfer). The file
        # owner grants it only when every input of the file allows it too; public stays off.
        default_access=FileAccess(api_transfer=True, public_disclosure=False),
    )
    return Workplace(
        files=files,
        skills=SkillLibrary(rt.store, files, clock, principal=PRINCIPALS[Owner.SKILLS]),
        knowledge=KnowledgeBase(files),
    )


async def library(rt: Runtime, scope: Scope) -> WorkplaceLibrary:
    """What the operator may see, through the same guard as the agent's own reads."""
    place = rt.workplace
    if place is None:
        raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
    area = stable_scope(scope)
    complete = True
    files = []
    for name in LIBRARY_AREAS:
        rows = await place.files.list(area, name, limit=200)
        complete = complete and len(rows) < 200
        files += rows
    skills = []
    for skill in await place.skills.all(area):
        try:
            await place.files.guard.check(area, (), (skill.method,))
        except ContractError as error:
            if error.error.code in HIDDEN:
                continue
            raise
        skills.append(skill)
    complete = complete and len(skills) <= 200
    return WorkplaceLibrary(
        scope=scope,
        skills=skills[:200],
        files=files,
        complete=complete,
        checked_at=rt.supervisor.clock.utc(),
    )
