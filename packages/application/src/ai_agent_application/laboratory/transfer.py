"""Explicit transfer of *selected settings* from a test scope to a work scope.

Only named settings move, after a preview (diff and consequences) and an operator decision that
carries the preview's digest. Memory, sources, episodes, people, relationships, claims and the live
character state are never copied: a test biography does not become a work biography. The profile
goes through the persona owner's normal create/apply path (digest handshake, state compatibility),
so a rejected or incompatible revision changes nothing.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ai_agent_contracts.api import CreateCommand, EditCommand
from ai_agent_contracts.codec import ContractError, from_wire
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode
from pydantic import TypeAdapter

from ai_agent_application.persona import PersonaService, import_markdown
from ai_agent_application.persona.rules import diff_profiles, state_blockers
from ai_agent_application.persona.schemas import ValueMap

type Key = Literal["profile", "gm_intensity", "story_rules_version"]
NEVER_TRANSFERRED = (
    "sources",
    "episodes",
    "claims",
    "people",
    "relationships",
    "topics_and_promises",
    "character_state",
    "usage_and_reports",
)
KEYS: tuple[str, ...] = ("profile", "gm_intensity", "story_rules_version")


class SettingsHolder(Protocol):
    """Non-profile settings of a scope (composition owns where they live)."""

    def get(self, scope: Scope, key: str) -> Any: ...

    def set(self, scope: Scope, key: str, value: Any) -> None: ...


@dataclass(frozen=True)
class TransferItem:
    key: str
    before: Any
    after: Any
    changes: tuple[str, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class TransferPreview:
    items: tuple[TransferItem, ...]
    never_transferred: tuple[str, ...]
    digest: str

    @property
    def blocked(self) -> bool:
        return any(item.blockers for item in self.items)


@dataclass(frozen=True)
class TransferResult:
    applied: tuple[str, ...]
    preview: TransferPreview


def _digest(items: Sequence[TransferItem], source: Scope, target: Scope) -> str:
    material = [
        list(source.partition),
        list(target.partition),
        [[i.key, repr(i.before), repr(i.after), list(i.changes), list(i.blockers)] for i in items],
    ]
    return hashlib.sha256(json.dumps(material, ensure_ascii=False).encode()).hexdigest()


class SettingsTransfer:
    def __init__(self, persona: PersonaService, settings: SettingsHolder) -> None:
        self.persona, self.settings = persona, settings

    @staticmethod
    def _check(source: Scope, target: Scope, keys: Sequence[str]) -> None:
        if (
            source.memory_mode != "test"
            or target.memory_mode != "work"
            or source.workspace_id != target.workspace_id
            or not keys
            or any(key not in KEYS for key in keys)
            or len(set(keys)) != len(keys)
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)

    async def preview(self, source: Scope, target: Scope, keys: Sequence[str]) -> TransferPreview:
        self._check(source, target, keys)
        items: list[TransferItem] = []
        for key in keys:
            if key == "profile":
                items.append(await self._profile_item(source, target))
            else:
                before = self.settings.get(target, key)
                after = self.settings.get(source, key)
                changes = () if before == after else (f"{key}: {before!r} -> {after!r}",)
                items.append(TransferItem(key, before, after, changes, ()))
        return TransferPreview(tuple(items), NEVER_TRANSFERRED, _digest(items, source, target))

    async def _profile_item(self, source: Scope, target: Scope) -> TransferItem:
        markdown = await self.persona.export_markdown(source)
        if markdown is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        parsed, _ = import_markdown(markdown)
        assert parsed is not None
        current = await self.persona.get_profile(target)
        if current is None:
            return TransferItem(
                "profile", None, parsed.revision_id, ("создаётся новый рабочий профиль",), ()
            )
        old_markdown = await self.persona.export_markdown(target)
        old, _ = import_markdown(old_markdown or "")
        assert old is not None
        diff = diff_profiles(old, parsed)
        changes = tuple(
            [
                *(["канон изменён"] if diff.canon_changed else []),
                *(["стиль изменён"] if diff.style_changed else []),
                *(f"добавлено: {name}" for name in diff.properties_added),
                *(f"удалено: {name}" for name in diff.properties_removed),
                *(f"изменено: {name}" for name in diff.properties_changed),
                *(f"начальное значение: {name}" for name in diff.initial_values_changed),
                *(f"границы развития: {name}" for name in diff.bounds_changed),
            ]
        )
        state = await self.persona.get_state(target)
        values: dict[str, float | str] = {}
        if state is not None:
            parsed_values = self.persona.extensions.validate(
                state.values.schema_id, state.values.schema_version, state.values.data
            )
            assert isinstance(parsed_values, ValueMap)
            values = dict(parsed_values.values)
        blockers = tuple(i.code + ":" + (i.section or "") for i in state_blockers(parsed, values))
        return TransferItem(
            "profile", current.accepted_revision_id, parsed.revision_id, changes, blockers
        )

    async def transfer(
        self,
        source: Scope,
        target: Scope,
        keys: Sequence[str],
        operator_decision_id: str,
        preview_digest: str,
    ) -> TransferResult:
        preview = await self.preview(source, target, keys)
        if not operator_decision_id or preview.digest != preview_digest or preview.blocked:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        applied: list[str] = []
        for item in preview.items:
            if item.key == "profile":
                await self._apply_profile(source, target, operator_decision_id)
            else:
                self.settings.set(target, item.key, item.after)
            applied.append(item.key)
        return TransferResult(tuple(applied), preview)

    async def _apply_profile(self, source: Scope, target: Scope, decision: str) -> None:
        markdown = await self.persona.export_markdown(source)
        assert markdown is not None
        parsed, _ = import_markdown(markdown)
        assert parsed is not None
        current = await self.persona.get_profile(target)
        ext = self.persona.extensions
        if current is None:
            payload = {
                "request_id": f"xfer-{decision}"[:120],
                "scope": target.model_dump(mode="json"),
                "target": target.persona_id,
                "type": "persona.create",
                "arguments": {
                    "schema_id": "persona.create-args",
                    "schema_version": "1.0",
                    "data": {"markdown": markdown},
                },
            }
            create = from_wire(
                TypeAdapter(CreateCommand), json.dumps(payload).encode(), extensions=ext
            )
            await self.persona.create(create)
            return
        edit_payload = {
            "request_id": f"xfer-{decision}"[:120],
            "scope": target.model_dump(mode="json"),
            "target": target.persona_id,
            "type": "persona.apply",
            "expected_version": current.version,
            "arguments": {
                "schema_id": "persona.apply-args",
                "schema_version": "1.0",
                "data": {"markdown": markdown, "revision_digest": parsed.digest},
            },
        }
        edit = from_wire(
            TypeAdapter(EditCommand), json.dumps(edit_payload).encode(), extensions=ext
        )
        await self.persona.apply(edit)


class MemorySettings:
    """Plain in-memory ``SettingsHolder`` (lab and tests; real storage is a composition choice)."""

    def __init__(self) -> None:
        self.values: dict[tuple[tuple[str, ...], str], Any] = {}

    def get(self, scope: Scope, key: str) -> Any:
        return self.values.get((scope.partition, key))

    def set(self, scope: Scope, key: str, value: Any) -> None:
        self.values[scope.partition, key] = value
