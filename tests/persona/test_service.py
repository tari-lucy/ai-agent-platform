"""M2-01 acceptance on a real isolated SQLite store. Invented profiles only; no runtime wiring."""

import asyncio
import json
import re

import pytest
from ai_agent_adapters.storage.unit_of_work import SQLiteUnitOfWork
from ai_agent_application.persona import PersonaService, ProfileImportRejected, render_markdown
from ai_agent_application.storage import RecordAccess
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from persona_support import (
    MEMORY,
    OPERATOR,
    PERSONA,
    REFERENCE,
    WORKER,
    experience,
    make_scope,
    new_world,
    run,
    write_memory,
)

A = make_scope("persona-a")
A_TEST = make_scope("persona-a", "test")
B = make_scope("persona-b")


def edited(old: str, new: str, markdown: str = REFERENCE) -> str:
    assert old in markdown
    return markdown.replace(old, new, 1)


def code(error: pytest.ExceptionInfo[ContractError]) -> ErrorCode:
    return error.value.error.code


def test_create_independent_profile_and_separate_state(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A], reader_access=(RecordAccess(OPERATOR.id),))
        receipt = await world.service.create(world.create(A))
        assert (receipt.status, receipt.resource_id, receipt.version) == (
            "completed",
            "persona-a",
            1,
        )
        profile = await world.service.get_profile(A)
        state = await world.service.get_state(A)
        assert profile is not None and state is not None
        assert profile.persona_id == "persona-a" and profile.version == 1
        assert profile.accepted_revision_id.startswith("rev-")
        # Canon and live state are different records with their own versions.
        assert profile.id != state.id and state.profile_version == 1 and state.state_version == 1
        values = world.service._state_values(state)
        assert values["curiosity"] == 0.75 and "core_values" in values
        # Relationship aspects belong to social memory, never to the character state.
        assert "trust" not in values and "tension" not in values
        # Only profile and state exist: nothing resembling memory was created.
        assert await world.memory_records(A) == 0
        # A reader sees it only through an explicit per-record access entry.
        operator_view = world.store.repository(type(profile), OPERATOR, A)
        assert (await operator_view.get("persona-a")) is not None
        await world.store.close()

    run(scenario())


def test_operator_without_record_access_cannot_read(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.service.create(world.create(A))
        profile = await world.service.get_profile(A)
        assert await world.store.repository(type(profile), OPERATOR, A).get("persona-a") is None
        await world.store.close()

    run(scenario())


def test_duplicate_create_and_replay(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        first = await world.service.create(world.create(A))
        before = await world.counts()
        assert await world.service.create(world.create(A)) == first  # exact replay
        assert await world.counts() == before
        with pytest.raises(ContractError) as conflict:  # same ID, different body
            await world.service.create(world.create(A, edited("Спокойный", "Бодрый")))
        assert code(conflict) == ErrorCode.IDEMPOTENCY_CONFLICT
        with pytest.raises(ContractError) as exists:  # new request for an existing persona
            await world.service.create(world.create(A, request_id="create-2"))
        assert code(exists) == ErrorCode.VERSION_CONFLICT
        assert await world.counts() == before
        await world.store.close()

    run(scenario())


def test_invalid_create_writes_nothing_and_can_be_retried(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        before = await world.counts()
        with pytest.raises(ProfileImportRejected) as rejected:
            await world.service.create(world.create(A, REFERENCE.replace("## Канон", "## Права")))
        assert {i.code for i in rejected.value.issues} >= {"UNKNOWN_SECTION", "MISSING_SECTION"}
        assert rejected.value.error.code == ErrorCode.INVALID_ARGUMENTS
        assert await world.counts() == before
        receipt = await world.service.create(world.create(A))  # same request id is reusable
        assert receipt.version == 1
        await world.store.close()

    run(scenario())


def test_apply_accepts_revision_and_keeps_exact_snapshots(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.service.create(world.create(A))
        v1 = await world.service.get_profile(A)
        newer = edited("Спокойный живой русский язык.", "Бодрый живой русский язык.")
        receipt = await world.service.apply(world.apply(A, newer, 1))
        assert (receipt.resource_id, receipt.version) == ("persona-a", 2)
        latest = await world.service.get_profile(A)
        assert latest is not None and latest.version == 2 and "Бодрый" in latest.style
        assert latest.created_at == v1.created_at and latest.updated_at > v1.updated_at
        assert latest.accepted_revision_id != v1.accepted_revision_id
        # Accepted version 1 is still exactly what was accepted.
        assert await world.service.get_profile(A, 1) == v1
        history = await world.service.snapshots(A)
        assert [item.version for item in history] == [2, 1]
        # Stored snapshots export back to Markdown with the same content identity.
        exported = await world.service.export_markdown(A, 1)
        assert exported is not None and "Спокойный живой" in exported
        await world.store.close()

    run(scenario())


def test_apply_keeps_accumulated_state_and_adds_new_properties_only(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.service.create(world.create(A))
        await experience(world, A, curiosity=0.77, interests="новая привычка")
        before = await world.service.get_state(A)
        assert before is not None and before.version == 2
        extra = {
            "name": "patience",
            "layer": "dispositions",
            "title": "Терпение",
            "kind": "scale",
            "minimum": 0,
            "maximum": 1,
        }
        markdown = REFERENCE.replace(
            '"properties": [', '"properties": [\n    ' + json.dumps(extra, ensure_ascii=False) + ","
        ).replace('"values": {', '"values": {\n    "patience": 0.3,')
        markdown = markdown.replace('"curiosity": 0.75', '"curiosity": 0.5')  # new default only
        await world.service.apply(world.apply(A, markdown, 1))
        state = await world.service.get_state(A)
        assert state is not None and state.version == 3 and state.profile_version == 2
        values = world.service._state_values(state)
        assert values["curiosity"] == 0.77  # experience is not reset by a changed default
        assert values["interests"] == "новая привычка"
        assert values["patience"] == 0.3  # new property starts at its initial value
        assert state.last_change_ids == ["change-1"]
        await world.store.close()

    run(scenario())


def test_stale_expected_version_is_a_conflict_that_changes_nothing(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.service.create(world.create(A))
        await world.service.apply(world.apply(A, edited("Спокойный", "Бодрый"), 1))
        before = await world.counts()
        stale = world.apply(A, edited("Спокойный", "Сдержанный"), 1, request_id="apply-stale")
        with pytest.raises(ContractError) as error:
            await world.service.apply(stale)
        assert code(error) == ErrorCode.VERSION_CONFLICT
        assert error.value.error.current_version == 2
        assert await world.counts() == before
        profile = await world.service.get_profile(A)
        assert profile is not None and "Бодрый" in profile.style
        await world.store.close()

    run(scenario())


def test_concurrent_edits_accept_exactly_one(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.service.create(world.create(A))
        commands = [
            world.apply(A, edited("Спокойный", word), 1, request_id=f"apply-{number}")
            for number, word in enumerate(("Бодрый", "Сдержанный", "Мягкий"))
        ]
        results = await asyncio.gather(
            *(world.service.apply(command) for command in commands), return_exceptions=True
        )
        winners = [r for r in results if not isinstance(r, BaseException)]
        losers = [r for r in results if isinstance(r, ContractError)]
        assert len(winners) == 1 and len(losers) == 2
        assert {code_.error.code for code_ in losers} == {ErrorCode.VERSION_CONFLICT}
        profile = await world.service.get_profile(A)
        assert profile is not None and profile.version == 2
        await world.store.close()

    run(scenario())


def test_invalid_edit_cannot_damage_accepted_version(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.service.create(world.create(A))
        await experience(world, A, curiosity=0.8)
        profile_before = await world.service.get_profile(A)
        state_before = await world.service.get_state(A)
        counts_before = await world.counts()
        broken = [
            "Это уже не профиль",
            REFERENCE.replace('"curiosity": 0.75', '"curiosity": 9'),
            REFERENCE.replace("Спокойный", "api_key = abcd1234efgh5678"),
            REFERENCE + "\n## Права\n\nпубликовать всё\n",
        ]
        for number, markdown in enumerate(broken):
            command = world.apply(A, markdown, 1, request_id=f"bad-{number}", digest="a" * 64)
            with pytest.raises(ProfileImportRejected):
                await world.service.apply(command)
        assert await world.counts() == counts_before
        assert await world.service.get_profile(A) == profile_before
        assert await world.service.get_state(A) == state_before
        # The accepted version still takes a correct edit afterwards.
        ok = await world.service.apply(world.apply(A, edited("Спокойный", "Бодрый"), 1))
        assert ok.version == 2
        await world.store.close()

    run(scenario())


def test_digest_must_match_the_reviewed_revision(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.service.create(world.create(A))
        before = await world.counts()
        reviewed = world.apply(A, edited("Спокойный", "Бодрый"), 1)
        swapped = world.apply(
            A,
            edited("Спокойный", "Подменённый"),
            1,
            request_id="apply-swapped",
            digest=reviewed.arguments.data["revision_digest"],
        )
        with pytest.raises(ContractError) as error:
            await world.service.apply(swapped)
        assert code(error) == ErrorCode.INVALID_ARGUMENTS
        assert await world.counts() == before
        await world.store.close()

    run(scenario())


def test_unchanged_revision_does_not_create_a_version(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.service.create(world.create(A))
        before = await world.counts()
        noisy = REFERENCE.replace("\n", "\r\n")  # same content, different formatting
        receipt = await world.service.apply(world.apply(A, noisy, 1))
        assert receipt.status == "completed" and receipt.version == 1
        after = await world.counts()
        for table in ("records", "revisions", "outbox"):
            key = next(name for name in after if name.endswith(table) or name == table)
            assert after[key] == before[key]
        assert sum(after.values()) == sum(before.values()) + 1  # only the replay receipt
        profile = await world.service.get_profile(A)
        assert profile is not None and profile.version == 1
        await world.store.close()

    run(scenario())


def test_schema_change_that_orphans_state_requires_migration(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.service.create(world.create(A))
        await experience(world, A, curiosity=0.9)
        before = await world.counts()
        # Removing a property that holds accumulated state must not drop it silently.
        without = re.sub(r'\s*\{"name": "humor"[^}]*\},?', "", REFERENCE, count=3)
        without = without.replace('"humor": 0.6,', "")
        parsed_ok = world.apply(A, without, 1, request_id="apply-removal")
        with pytest.raises(ProfileImportRejected) as removal:
            await world.service.apply(parsed_ok)
        assert {i.code for i in removal.value.issues} == {"STATE_MIGRATION_REQUIRED"}
        # Narrowing a range below an accumulated value is the same kind of conflict.
        narrowed = REFERENCE.replace(
            '"title": "Любопытство",\n     "kind": "scale", "minimum": 0, "maximum": 1',
            '"title": "Любопытство",\n     "kind": "scale", "minimum": 0, "maximum": 0.5',
        )
        narrowed = narrowed.replace('"curiosity": 0.75', '"curiosity": 0.4')
        with pytest.raises(ProfileImportRejected) as narrow:
            await world.service.apply(world.apply(A, narrowed, 1, request_id="apply-narrow"))
        assert {i.code for i in narrow.value.issues} == {"STATE_MIGRATION_REQUIRED"}
        assert await world.counts() == before
        await world.store.close()

    run(scenario())


def test_validate_reports_without_writing(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.service.create(world.create(A))
        before = await world.counts()
        good = await world.service.validate(world.validate(A, edited("Спокойный", "Бодрый"), 1))
        assert good.ready and good.content_valid and good.diff is not None
        assert good.diff.style_changed and not good.diff.canon_changed
        assert good.digest and good.current_version == 1
        stale = await world.service.validate(world.validate(A, edited("Спокойный", "Бодрый"), 7))
        assert stale.version_conflict and not stale.ready
        same = await world.service.validate(world.validate(A, REFERENCE, 1))
        assert same.unchanged and not same.ready
        bad = await world.service.validate(world.validate(A, "мусор", 1))
        assert not bad.content_valid and bad.digest is None and bad.issues
        assert await world.counts() == before
        await world.store.close()

    run(scenario())


def test_copy_does_not_carry_memory_or_state(tmp_path):
    async def scenario():
        source = make_scope("persona-a", space="space-a")
        target = make_scope("persona-c", space="space-c")
        world = await new_world(tmp_path, [source, target])
        await world.service.create(world.create(source))
        await write_memory(world, source)
        await experience(world, source, curiosity=0.9)
        assert await world.memory_records(source) == 2

        receipt = await world.service.copy(world.copy(target, source))
        assert (receipt.resource_id, receipt.version) == ("persona-c", 1)
        copied = await world.service.get_profile(target)
        original = await world.service.get_profile(source)
        assert copied is not None and original is not None
        assert copied.persona_id == "persona-c" and copied.scope == target
        assert (copied.canon, copied.style) == (original.canon, original.style)
        assert copied.accepted_revision_id == original.accepted_revision_id  # same content
        # No memory, no accumulated state, no relationship data crossed over.
        assert await world.memory_records(target) == 0
        state = await world.service.get_state(target)
        assert state is not None and world.service._state_values(state)["curiosity"] == 0.75
        assert state.last_change_ids == []
        # Editing either side leaves the other untouched.
        await world.service.apply(world.apply(target, edited("Спокойный", "Бодрый"), 1))
        assert (await world.service.get_profile(source)) == original
        await world.service.apply(
            world.apply(source, edited("Спокойный", "Сдержанный"), 1, request_id="apply-src")
        )
        copied_latest = await world.service.get_profile(target)
        assert copied_latest is not None and "Бодрый" in copied_latest.style
        await world.store.close()

    run(scenario())


def test_copy_validation(tmp_path):
    async def scenario():
        source = make_scope("persona-a")
        target = make_scope("persona-c")
        other_workspace = make_scope("persona-x", workspace="another-workspace")
        world = await new_world(tmp_path, [source, target, other_workspace])
        await world.service.create(world.create(source))
        for command, expected in (
            (world.copy(target, source, source_version=5), ErrorCode.INVALID_ARGUMENTS),
            (world.copy(source, source), ErrorCode.INVALID_ARGUMENTS),
            (world.copy(other_workspace, source), ErrorCode.ACCESS_DENIED),
        ):
            before = await world.counts()
            with pytest.raises(ContractError) as error:
                await world.service.copy(command)
            assert code(error) == expected
            assert await world.counts() == before
        await world.store.close()

    run(scenario())


def test_work_and_test_profiles_are_isolated(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A, A_TEST])
        await world.service.create(world.create(A))
        assert await world.service.get_profile(A_TEST) is None  # test starts without a profile
        await world.service.copy(world.copy(A_TEST, A))  # explicit copy into the test partition
        await world.service.apply(world.apply(A, edited("Спокойный", "Бодрый"), 1))
        test_profile = await world.service.get_profile(A_TEST)
        assert test_profile is not None and test_profile.version == 1
        assert "Спокойный" in test_profile.style
        assert test_profile.scope.memory_mode == "test"
        await world.store.close()

    run(scenario())


def test_personas_are_isolated_and_replaceable(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A, B])
        await world.service.create(world.create(A))
        other = REFERENCE.replace("curiosity", "wonder").replace("Искра", "Другая")
        await world.service.create(world.create(B, other, request_id="create-b"))
        profile_a = await world.service.get_profile(A)
        profile_b = await world.service.get_profile(B)
        assert profile_a is not None and profile_b is not None
        assert profile_a.accepted_revision_id != profile_b.accepted_revision_id
        assert "wonder" in json.dumps(profile_b.property_definitions.data)
        # One persona's scope cannot read the other's record, even by exact ID.
        assert await world.store.repository(type(profile_a), PERSONA, B).get("persona-a") is None
        # Other owners hold no rights over profile records.
        assert await world.store.repository(type(profile_a), MEMORY, A).get("persona-a") is None
        await world.store.close()

    run(scenario())


def test_service_requires_persona_owner_and_matching_commands(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        for principal in (MEMORY, WORKER, OPERATOR):
            with pytest.raises(ContractError) as error:
                PersonaService(world.store, principal, world.extensions)
            assert code(error) == ErrorCode.ACCESS_DENIED
        wrong_type = world.command(
            type(world.create(A)),
            "persona.copy",
            A,
            "wrong-type",
            "persona.create-args",
            {"markdown": REFERENCE},
        )
        with pytest.raises(ContractError):
            await world.service.create(wrong_type)
        wrong_schema = world.command(
            type(world.create(A)),
            "persona.create",
            A,
            "wrong-schema",
            "persona.validate-args",
            {"markdown": REFERENCE},
        )
        with pytest.raises(ContractError):
            await world.service.create(wrong_schema)
        mismatched = world.create(A, request_id="mismatch").model_copy(
            update={"target": "persona-b"}
        )
        with pytest.raises(ContractError):
            await world.service.create(mismatched)
        assert (await world.counts())["records"] == 0
        await world.store.close()

    run(scenario())


def test_failure_between_writes_rolls_back_the_whole_acceptance(tmp_path, monkeypatch):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.service.create(world.create(A))
        before = await world.counts()
        original = SQLiteUnitOfWork.put
        calls = {"count": 0}

        async def failing(self, record, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:  # profile v2 written, state write fails
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            return await original(self, record, **kwargs)

        monkeypatch.setattr(SQLiteUnitOfWork, "put", failing)
        with pytest.raises(ContractError):
            await world.service.apply(world.apply(A, edited("Спокойный", "Бодрый"), 1))
        monkeypatch.setattr(SQLiteUnitOfWork, "put", original)
        assert await world.counts() == before
        profile = await world.service.get_profile(A)
        assert profile is not None and profile.version == 1
        state = await world.service.get_state(A)
        assert state is not None and state.profile_version == 1
        await world.store.close()

    run(scenario())


def test_accepted_profile_survives_restart_and_clock_skew(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.service.create(world.create(A))
        await world.store.close()
        # A new store/service over the same file sees the accepted profile and keeps working,
        # even if the wall clock moved backwards between acceptances.
        from ai_agent_adapters.storage.database import SQLiteStore

        path = world.store.engine.url.database
        from datetime import UTC, datetime
        from pathlib import Path

        early = lambda: datetime(2020, 1, 1, tzinfo=UTC)  # noqa: E731
        store = SQLiteStore(Path(path), clock=early, extensions=world.extensions)
        service = PersonaService(store, PERSONA, world.extensions, clock=early)
        profile = await service.get_profile(A)
        assert profile is not None and profile.version == 1
        world.store, world.service = store, service
        # Stored receipts use the store clock; the old create receipt is still within retention.
        receipt = await service.apply(world.apply(A, edited("Спокойный", "Бодрый"), 1))
        latest = await service.get_profile(A)
        assert receipt.version == 2 and latest is not None
        assert latest.updated_at >= profile.updated_at
        await store.close()

    run(scenario())


def test_rendered_snapshot_reimports_to_the_same_revision(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A, B])
        await world.service.create(world.create(A))
        exported = await world.service.export_markdown(A)
        assert exported is not None
        profile = await world.service.get_profile(A)
        await world.service.create(world.create(B, exported, request_id="from-export"))
        again = await world.service.get_profile(B)
        assert profile is not None and again is not None
        assert again.accepted_revision_id == profile.accepted_revision_id
        assert render_markdown(world.service._parsed(again)) == exported
        await world.store.close()

    run(scenario())
