"""Persona owner: profile lifecycle, Markdown import and accepted-revision snapshots.

Accepted revisions are the immutable ``Profile`` versions of the M1-02 store: version *n* stays
readable after *n+1* is accepted. A proposed edit lives in the operator's Markdown project until
accepted, so a rejected import never has a stored representation that could damage an accepted
version. Live character state (``CharacterState``) is a separate record owned by the same service;
applying a revision keeps its accumulated values and never resets them silently.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ai_agent_contracts.api import CreateCommand, EditCommand, Receipt
from ai_agent_contracts.codec import ContractError, from_wire
from ai_agent_contracts.common import Contract, ExtensionPayload, Scope
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.persona import CharacterState, Profile
from ai_agent_contracts.states import ErrorCode
from pydantic import TypeAdapter

from ai_agent_application.storage import Owner, Principal, RecordAccess, Storage, UnitOfWork

from .markdown import parse_markdown, render_markdown
from .model import Issue, ParsedProfile, ProfileDiff, ProfileImportRejected
from .rules import (
    check_profile,
    diff_profiles,
    initial_state_values,
    merged_state_values,
    state_blockers,
)
from .schemas import (
    ApplyArguments,
    CopyArguments,
    CreateArguments,
    DevelopmentBounds,
    PropertyDefinitions,
    ValidateArguments,
    ValueMap,
)

MAX_PERSONA_ID_CHARS = 120  # state record id is "state:" + persona id within the 128 limit


def import_markdown(markdown: str) -> tuple[ParsedProfile | None, tuple[Issue, ...]]:
    """Pure format + consistency check; touches no storage."""
    parsed, issues = parse_markdown(markdown)
    if parsed is None:
        return None, issues
    found = check_profile(parsed)
    return (None, found) if found else (parsed, ())


@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[Issue, ...]
    blockers: tuple[Issue, ...]
    digest: str | None
    current_version: int | None
    version_conflict: bool
    unchanged: bool
    diff: ProfileDiff | None

    @property
    def content_valid(self) -> bool:
        return self.digest is not None and not self.issues

    @property
    def ready(self) -> bool:
        """Apply would be accepted if sent now with the reported digest."""
        return (
            self.content_valid
            and not self.blockers
            and not self.version_conflict
            and not self.unchanged
            and self.current_version is not None
        )


class PersonaService:
    def __init__(
        self,
        storage: Storage,
        principal: Principal,
        extensions: ExtensionRegistry,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        reader_access: tuple[RecordAccess, ...] = (),
    ) -> None:
        if principal.role != "core_owner" or principal.owner is not Owner.PERSONA:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        self.storage, self.principal, self.extensions = storage, principal, extensions
        self.clock, self.reader_access = clock, reader_access

    # ---- reads ----------------------------------------------------------------------------

    async def get_profile(self, scope: Scope, version: int | None = None) -> Profile | None:
        repository = self.storage.repository(Profile, self.principal, scope)
        return await repository.get(scope.persona_id, version=version)

    async def snapshots(self, scope: Scope, *, limit: int = 50) -> list[Profile]:
        """Accepted revisions, newest first; each is the exact stored snapshot."""
        current = await self.get_profile(scope)
        if current is None:
            return []
        found: list[Profile] = []
        for number in range(current.version, max(0, current.version - limit), -1):
            snapshot = await self.get_profile(scope, number)
            if snapshot is not None:
                found.append(snapshot)
        return found

    async def get_state(self, scope: Scope) -> CharacterState | None:
        repository = self.storage.repository(CharacterState, self.principal, scope)
        return await repository.get(self._state_id(scope.persona_id))

    async def export_markdown(self, scope: Scope, version: int | None = None) -> str | None:
        profile = await self.get_profile(scope, version)
        return None if profile is None else render_markdown(self._parsed(profile))

    async def validate(self, command: EditCommand) -> ValidationReport:
        """Preview an edit against the current accepted version. Writes nothing."""
        self._check(command, "persona.validate")
        arguments = self._arguments(command.arguments, "persona.validate-args", ValidateArguments)
        parsed, issues = import_markdown(arguments.markdown)
        current = await self.get_profile(command.scope)
        if current is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        blockers: tuple[Issue, ...] = ()
        diff: ProfileDiff | None = None
        unchanged = False
        if parsed is not None:
            state = await self.get_state(command.scope)
            blockers = state_blockers(parsed, self._state_values(state))
            diff = diff_profiles(self._parsed(current), parsed)
            unchanged = current.accepted_revision_id == parsed.revision_id
        return ValidationReport(
            issues=issues,
            blockers=blockers,
            digest=parsed.digest if parsed else None,
            current_version=current.version,
            version_conflict=command.expected_version != current.version,
            unchanged=unchanged,
            diff=diff,
        )

    # ---- commands ---------------------------------------------------------------------------

    async def create(self, command: CreateCommand) -> Receipt:
        """persona.create: a new independent profile and fresh state; no memory is involved."""
        self._check(command, "persona.create")
        async with self.storage.transaction(self.principal, command.scope, command) as unit:
            if unit.replay is not None:
                return unit.replay
            arguments = self._arguments(command.arguments, "persona.create-args", CreateArguments)
            parsed = self._import(arguments.markdown, command.request_id)
            receipt = await self._write_new(unit, command, parsed)
            return receipt

    async def copy(self, command: CreateCommand) -> Receipt:
        """persona.copy: explicit target scope; only an accepted snapshot of canon is copied.

        State values, relationships and every memory record stay behind. The target starts from
        the copied profile's initial values.
        """
        self._check(command, "persona.copy")
        scope = command.scope
        async with self.storage.transaction(self.principal, scope, command) as unit:
            if unit.replay is not None:
                return unit.replay
            arguments = self._arguments(command.arguments, "persona.copy-args", CopyArguments)
            source_scope = arguments.source_scope
            if source_scope.workspace_id != scope.workspace_id:
                raise ContractError(ErrorCode.ACCESS_DENIED, command.request_id)
            if source_scope.partition == scope.partition:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
            source = await self.get_profile(source_scope, arguments.source_version)
            if source is None:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
            parsed = self._parsed(source)
            found = check_profile(parsed)
            if found:
                raise ProfileImportRejected(found, command.request_id)
            return await self._write_new(unit, command, parsed)

    async def apply(self, command: EditCommand) -> Receipt:
        """persona.apply: accept one validated revision as the next profile version."""
        self._check(command, "persona.apply")
        scope = command.scope
        async with self.storage.transaction(self.principal, scope, command) as unit:
            if unit.replay is not None:
                return unit.replay
            arguments = self._arguments(command.arguments, "persona.apply-args", ApplyArguments)
            current = await self.get_profile(scope)
            if current is None:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
            if command.expected_version != current.version:
                raise ContractError(ErrorCode.VERSION_CONFLICT, command.request_id, current.version)
            parsed = self._import(arguments.markdown, command.request_id)
            if parsed.digest != arguments.revision_digest:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
            if current.accepted_revision_id == parsed.revision_id:
                receipt = Receipt(
                    request_id=command.request_id,
                    scope=scope,
                    status="completed",
                    operation_id=command.request_id,
                    version=current.version,
                )
                await unit.complete(receipt)
                return receipt
            state = await self.get_state(scope)
            blockers = state_blockers(parsed, self._state_values(state))
            if blockers:
                raise ProfileImportRejected(blockers, command.request_id)
            now = max(self.clock(), current.updated_at)
            version = current.version + 1
            await unit.put(
                self._profile_record(scope, parsed, version, current.created_at, now),
                expected_version=current.version,
                access=self.reader_access or None,
            )
            values = merged_state_values(parsed, self._state_values(state))
            if state is None:
                await unit.put(
                    self._state_record(scope, values, 1, version, [], now, now),
                    access=self.reader_access or None,
                )
            else:
                await unit.put(
                    self._state_record(
                        scope,
                        values,
                        state.version + 1,
                        version,
                        list(state.last_change_ids),
                        state.created_at,
                        max(now, state.updated_at),
                    ),
                    expected_version=state.version,
                    access=self.reader_access or None,
                )
            receipt = Receipt(
                request_id=command.request_id,
                scope=scope,
                status="completed",
                resource_id=scope.persona_id,
                version=version,
            )
            await unit.complete(receipt)
            return receipt

    # ---- internals --------------------------------------------------------------------------

    def _check(self, command: CreateCommand | EditCommand, expected: str) -> None:
        if (
            command.type != expected
            or command.target != command.scope.persona_id
            or len(command.scope.persona_id) > MAX_PERSONA_ID_CHARS
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)

    def _arguments[M: Contract](
        self, payload: ExtensionPayload, schema_id: str, model: type[M]
    ) -> M:
        if payload.schema_id != schema_id:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        try:
            parsed = self.extensions.validate(
                payload.schema_id, payload.schema_version, payload.data
            )
        except ValueError:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
        if not isinstance(parsed, model):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        return parsed

    def _import(self, markdown: str, request_id: str) -> ParsedProfile:
        parsed, issues = import_markdown(markdown)
        if parsed is None:
            raise ProfileImportRejected(issues, request_id)
        return parsed

    def _parsed(self, profile: Profile) -> ParsedProfile:
        return ParsedProfile(
            profile.canon,
            profile.style,
            self._arguments(
                profile.property_definitions, "persona.property-definitions", PropertyDefinitions
            ),
            self._arguments(profile.initial_values, "persona.initial-values", ValueMap),
            self._arguments(
                profile.development_bounds, "persona.development-bounds", DevelopmentBounds
            ),
        )

    def _state_values(self, state: CharacterState | None) -> dict[str, float | str]:
        if state is None:
            return {}
        return dict(self._arguments(state.values, "persona.state-values", ValueMap).values)

    @staticmethod
    def _state_id(persona_id: str) -> str:
        return "state:" + persona_id

    @staticmethod
    def _extension(schema_id: str, model: Contract) -> dict[str, object]:
        return {
            "schema_id": schema_id,
            "schema_version": "1.0",
            "data": model.model_dump(mode="json"),
        }

    def _record[R: Contract](self, model: type[R], payload: dict[str, object]) -> R:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return from_wire(TypeAdapter(model), encoded, extensions=self.extensions)

    def _profile_record(
        self,
        scope: Scope,
        parsed: ParsedProfile,
        version: int,
        created_at: datetime,
        updated_at: datetime,
    ) -> Profile:
        return self._record(
            Profile,
            {
                "id": scope.persona_id,
                "scope": scope.model_dump(mode="json"),
                "created_at": created_at.isoformat(),
                "updated_at": updated_at.isoformat(),
                "version": version,
                "persona_id": scope.persona_id,
                "canon": parsed.canon,
                "style": parsed.style,
                "property_definitions": self._extension(
                    "persona.property-definitions", parsed.definitions
                ),
                "initial_values": self._extension("persona.initial-values", parsed.initial_values),
                "development_bounds": self._extension("persona.development-bounds", parsed.bounds),
                "accepted_revision_id": parsed.revision_id,
            },
        )

    def _state_record(
        self,
        scope: Scope,
        values: ValueMap,
        version: int,
        profile_version: int,
        last_change_ids: list[str],
        created_at: datetime,
        updated_at: datetime,
    ) -> CharacterState:
        return self._record(
            CharacterState,
            {
                "id": self._state_id(scope.persona_id),
                "scope": scope.model_dump(mode="json"),
                "created_at": created_at.isoformat(),
                "updated_at": updated_at.isoformat(),
                "version": version,
                "persona_id": scope.persona_id,
                "profile_version": profile_version,
                "state_version": version,
                "values": self._extension("persona.state-values", values),
                "last_change_ids": last_change_ids,
            },
        )

    async def _write_new(
        self, unit: UnitOfWork, command: CreateCommand, parsed: ParsedProfile
    ) -> Receipt:
        scope = command.scope
        now = self.clock()
        await unit.put(
            self._profile_record(scope, parsed, 1, now, now), access=self.reader_access or None
        )
        await unit.put(
            self._state_record(scope, initial_state_values(parsed), 1, 1, [], now, now),
            access=self.reader_access or None,
        )
        receipt = Receipt(
            request_id=command.request_id,
            scope=scope,
            status="completed",
            resource_id=scope.persona_id,
            version=1,
        )
        await unit.complete(receipt)
        return receipt
