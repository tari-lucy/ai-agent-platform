"""Skills in a working session (MCP operations), over the existing SkillLibrary and ManagedFiles.

The person can find, read and save its own methods and use them in a later turn. What this
module never does: grant a permission, add a tool, run a script, or mark a skill verified — a
saved method is ``experimental``; verification needs confirmed bases, never the model's say-so.
Reads go through the file owner (area right of the attempt's capability, api_transfer,
versions, TTL, ledger); the caller records what was read in the WorkAttempt before the text is
returned and the written method among the attempt's results. Saves are CAS on the skill
version with a request key derived from the tool call, so a retry replays and STOP refuses.
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ai_agent_application.agent_skills import SkillDraft
from ai_agent_application.agent_workplace.ports import WorkAdmission
from ai_agent_contracts.agent_workplace import WorkAttempt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import ArtifactRef, SourceRef
from ai_agent_contracts.states import ErrorCode

from .workplace import granted_areas

if TYPE_CHECKING:
    from .service import Runtime
    from .work_session_runtime import Release

OPERATIONS: dict[str, set[str]] = {
    "find_skills": {"query"},
    "read_skill": {"skill_id"},
    "save_skill": {
        "skill_id",
        "title",
        "applicability",
        "limitations",
        "method",
        "change_reason",
        "expected_version",
    },
}
MAX_TEXT = 4000
HIDDEN = {ErrorCode.ACCESS_DENIED, ErrorCode.EXPIRED, ErrorCode.STALE_CONTEXT}

type Outcome = tuple[Any, list[SourceRef], list[ArtifactRef], list[ArtifactRef]]


async def call(
    rt: Runtime,
    guard: Release,
    admission: WorkAdmission,
    attempt: WorkAttempt,
    operation: str,
    arguments: Mapping[str, str],
    key: str,
) -> Outcome:
    """Returns (result, read sources, read files, written files) for the session bridge."""
    place = rt.workplace
    if place is None:
        raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
    if "skills" not in await granted_areas(rt, admission.scope, attempt.capability_id):
        raise ContractError(ErrorCode.ACCESS_DENIED)
    library = place.skills
    if operation == "find_skills":
        query = arguments.get("query", "").strip()
        if not query:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        sources: list[SourceRef] = []
        files: list[ArtifactRef] = []
        shown = []
        for skill in await library.find(admission.scope, query, limit=5):
            try:  # a skill whose method or evidence is closed is not shown at all
                await guard.verify(admission, skill.evidence, (skill.method,), "api_transfer")
            except ContractError as error:
                if error.error.code in HIDDEN:
                    continue
                raise
            sources += skill.evidence
            files.append(skill.method)
            shown.append(
                {
                    "skill_id": skill.skill_id,
                    "version": skill.version,
                    "status": skill.status,
                    "title": skill.title,
                    "applicability": skill.applicability[:300],
                    "limitations": skill.limitations[:300],
                }
            )
        return shown, sources, files, []
    if operation == "read_skill":
        skill, body = await library.read(admission, arguments.get("skill_id", ""))
        text = body.decode("utf-8", errors="replace")
        return (
            {
                "skill_id": skill.skill_id,
                "version": skill.version,
                "status": skill.status,
                "title": skill.title,
                "applicability": skill.applicability,
                "limitations": skill.limitations,
                "method": text[:MAX_TEXT],
                "truncated": len(text) > MAX_TEXT,
                # Descriptive only: a skill never grants a capability or runs anything.
                "uses_capabilities": list(skill.uses_capabilities),
            },
            list(skill.evidence),
            [skill.method],
            [],
        )
    if operation == "save_skill":
        try:
            expected = (
                int(arguments["expected_version"]) if arguments.get("expected_version") else None
            )
            draft = SkillDraft(
                skill_id=arguments["skill_id"],
                title=arguments["title"][:200],
                applicability=arguments["applicability"],
                limitations=arguments.get("limitations", "").strip() or "Не указаны.",
                change_reason=(arguments.get("change_reason", "").strip() or "Новая версия.")[:500],
            )
            method = arguments["method"]
        except KeyError, ValueError:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
        if not method.strip() or not draft.title.strip() or not draft.applicability.strip():
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        try:
            saved = await library.save(admission, key, draft, method.encode("utf-8"), expected)
        except ContractError:
            raise  # a conflict, refusal or STOP is the owner's answer, kept as it is
        except ValueError:  # an id or path the contracts refuse
            raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
        return (
            {"skill_id": saved.skill_id, "version": saved.version, "status": saved.status},
            [],
            [saved.method],
            [saved.method],
        )
    raise ContractError(ErrorCode.INVALID_ARGUMENTS)
