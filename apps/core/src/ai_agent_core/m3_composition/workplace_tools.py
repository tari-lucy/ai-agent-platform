"""Tool executors of the two library capabilities (AW-03 owners, AW-05 calls).

Trusted adapters: the model only names an operation and string arguments. Every call rechecks
the agent's current right for the area before and after (the file owner does it for each IO;
catalogue-wide find/search check it here), works through the skill/knowledge owners and returns
a bounded summary plus what it read and wrote. A failed check is a refusal, never a widening.
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from ai_agent_application.agent_activity.step import ToolExecutor, ToolResult
from ai_agent_application.agent_knowledge import NoteDraft
from ai_agent_application.agent_knowledge.service import NoteKind
from ai_agent_application.agent_skills import SkillDraft
from ai_agent_application.agent_workplace.ports import WorkAdmission
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode

from .workplace import Workplace, granted_areas

if TYPE_CHECKING:
    from .service import Runtime

SKILLS = "workplace.skills"
KNOWLEDGE = "workplace.knowledge"
MAX_SHOWN = 3000


def _int(value: str | None) -> int | None:
    if value is None or value.strip() in ("", "none", "null"):
        return None
    if not value.strip().isdigit():
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    return int(value)


def _need(arguments: Mapping[str, str], *names: str) -> list[str]:
    values = [arguments.get(name, "").strip() for name in names]
    if not all(values):
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    return values


class _Tool:
    capability = ""
    area = ""

    def __init__(self, rt: Runtime, place: Workplace) -> None:
        self.rt, self.place = rt, place

    async def _allowed(self, admission: WorkAdmission) -> None:
        if not admission.is_current():
            raise ContractError(ErrorCode.STALE_CONTEXT)
        if self.area not in await granted_areas(self.rt, admission.scope, self.capability):
            raise ContractError(ErrorCode.ACCESS_DENIED)

    async def call(
        self, admission: WorkAdmission, capability_id: str, arguments: Mapping[str, str]
    ) -> ToolResult:
        if capability_id != self.capability:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        try:
            return await self.run(admission, arguments.get("op", "").strip(), arguments)
        except ContractError as error:
            return ToolResult("failed", "Отказ: " + error.error.code.value)
        except ValueError:  # a path, id or size the contracts refuse
            return ToolResult("failed", "Отказ: " + ErrorCode.INVALID_ARGUMENTS.value)

    async def run(
        self, admission: WorkAdmission, op: str, arguments: Mapping[str, str]
    ) -> ToolResult:
        raise NotImplementedError


class SkillsTool(_Tool):
    capability, area = SKILLS, "skills"

    async def run(
        self, admission: WorkAdmission, op: str, arguments: Mapping[str, str]
    ) -> ToolResult:
        library = self.place.skills
        if op == "find":
            [query] = _need(arguments, "query")
            await self._allowed(admission)
            rows = await library.find(admission.scope, query, limit=5)
            await self._allowed(admission)
            lines = [
                f"{row.skill_id} v{row.version} ({row.status}): {row.title} — "
                f"{row.applicability[:200]}"
                for row in rows
            ]
            return ToolResult("succeeded", "\n".join(lines) or "Подходящих навыков нет.")
        if op == "read":
            [skill_id] = _need(arguments, "skill_id")
            skill, body = await library.read(admission, skill_id)
            text = body.decode("utf-8", errors="replace")[:MAX_SHOWN]
            return ToolResult(
                "succeeded",
                f"Навык {skill.skill_id} v{skill.version} ({skill.status}): {skill.title}\n"
                f"Ограничения: {skill.limitations[:500]}\nМетод:\n{text}",
                tuple(skill.evidence[:50]),
                (skill.method,),
            )
        if op == "save":
            skill_id, title, applicability, method = _need(
                arguments, "skill_id", "title", "applicability", "method"
            )
            saved = await library.save(
                admission,
                admission.attempt_id + ".skill",
                SkillDraft(
                    skill_id=skill_id,
                    title=title[:200],
                    applicability=applicability,
                    limitations=arguments.get("limitations", "").strip() or "Не указаны.",
                    change_reason=(arguments.get("change_reason", "").strip() or "Новая версия.")[
                        :500
                    ],
                ),
                method.encode("utf-8"),
                _int(arguments.get("expected_version")),
            )
            return ToolResult(
                "succeeded",
                f"Сохранён навык {saved.skill_id} v{saved.version} ({saved.status}).",
                written=(saved.method,),
            )
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)


class KnowledgeTool(_Tool):
    capability, area = KNOWLEDGE, "knowledge"

    async def run(
        self, admission: WorkAdmission, op: str, arguments: Mapping[str, str]
    ) -> ToolResult:
        knowledge = self.place.knowledge
        if op == "search":
            [query] = _need(arguments, "query")
            await self._allowed(admission)
            areas = await granted_areas(self.rt, admission.scope, self.capability)
            rows = [r for r in await knowledge.search(admission.scope, query) if r.area in areas]
            await self._allowed(admission)
            lines = [f"{row.id} ({row.kind}, {row.provenance}): {row.title}" for row in rows[:10]]
            return ToolResult("succeeded", "\n".join(lines) or "Ничего не найдено.")
        if op == "read":
            [file_id] = _need(arguments, "file_id")
            row, text = await knowledge.read(admission, file_id)
            return ToolResult(
                "succeeded",
                f"{row.title} ({row.kind}, {row.provenance}):\n{text[:MAX_SHOWN]}",
                tuple(row.dependencies[:50]),
                (row.reference,),
            )
        if op == "write":
            file_id, path, title, kind, body = _need(
                arguments, "file_id", "path", "title", "kind", "body"
            )
            if kind not in ("note", "hypothesis"):
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            written = await knowledge.write(
                admission,
                admission.attempt_id + ".note",
                NoteDraft(file_id, path, title[:200], cast(NoteKind, kind)),
                body,
                _int(arguments.get("expected_version")),
            )
            return ToolResult(
                "succeeded",
                f"Записано: {written.id} v{written.version} ({written.kind}).",
                written=(written.reference,),
            )
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)


def executors(rt: Runtime, place: Workplace) -> dict[str, ToolExecutor]:
    return {SKILLS: SkillsTool(rt, place), KNOWLEDGE: KnowledgeTool(rt, place)}
