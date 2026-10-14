"""People, platform identities, encounters and relationships (M2-03)."""

import pytest
from ai_agent_application.social import SocialService
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from social_support import (
    MEMORY,
    OPERATOR,
    WORKER,
    command,
    make_scope,
    new_world,
    run,
)

A = make_scope("persona-a")
A_TEST = make_scope("persona-a", "test")
B = make_scope("persona-b")


def code(info):
    return info.value.error.code


def test_the_same_nickname_on_other_accounts_or_platforms_is_another_person(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        nika, evidence = await world.person(A, "acct-1", "twitch", "Ника")
        homonym, _ = await world.person(A, "acct-2", "twitch", "Ника")
        other_platform, _ = await world.person(A, "acct-1", "youtube", "Ника")
        server_a, _ = await world.person(A, "uuid-9", "minecraft:server-a", "Ника")
        server_b, _ = await world.person(A, "uuid-9", "minecraft:server-b", "Ника")
        ids = {p.id for p in (nika, homonym, other_platform, server_a, server_b)}
        assert len(ids) == 5  # nickname, same account id on another platform/server: never merged
        again = await world.social.observe_account(command(A), "twitch", "acct-1", "Ника", evidence)
        assert again.person.id == nika.id and not again.created
        await world.store.close()

    run(scenario())


def test_anonymous_senders_have_no_identity_or_history(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        evidence = await world.source(A, "Кто-то написал в чат.")
        for account in ("", "   "):
            assert (
                await world.social.observe_account(
                    command(A), "twitch", account, "Аноним", evidence
                )
                is None
            )
            assert await world.social.identify(A, "twitch", account) is None
        assert (
            await world.social._all(
                __import__("ai_agent_contracts.memory", fromlist=["Person"]).Person, A
            )
            == []
        )
        await world.store.close()

    run(scenario())


def test_a_changed_nickname_keeps_the_person_and_history(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        person, evidence = await world.person(A, "acct-1", "twitch", "Ника")
        renamed = await world.social.observe_account(
            command(A), "twitch", "acct-1", "Ника-2", evidence
        )
        assert renamed.renamed and renamed.person.id == person.id and renamed.person.version == 2
        assert renamed.person.accounts[0].display_name == "Ника-2"
        # Somebody else now using the old nickname is not the old person.
        newcomer, _ = await world.person(A, "acct-77", "twitch", "Ника")
        assert newcomer.id != person.id
        await world.store.close()

    run(scenario())


def test_persona_and_work_test_scopes_never_share_people(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A, A_TEST, B])
        in_a, _ = await world.person(A, "acct-1")
        in_test, _ = await world.person(A_TEST, "acct-1")
        in_b, _ = await world.person(B, "acct-1")
        assert len({in_a.id, in_test.id, in_b.id}) == 3
        assert await world.social.identify(A_TEST, "twitch", "acct-1") is not None
        empty = make_scope("persona-empty")
        await world.provision(empty)
        assert await world.social.identify(empty, "twitch", "acct-1") is None
        assert await world.social.context_for(B, in_a.id) is None  # even by exact id
        await world.store.close()

    run(scenario())


def test_account_links_are_explicit_and_never_a_silent_merge(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        nika, evidence = await world.person(A, "acct-1", "twitch", "Ника")
        rival, _ = await world.person(A, "acct-9", "youtube", "Ника")
        social = world.social

        await social.link_account(
            command(A), nika.id, 1, "youtube", "yt-5", "Ника", evidence, confirmation="hypothesis"
        )
        assert await social.identify(A, "youtube", "yt-5") is None  # a guess identifies nobody

        await social.link_account(
            command(A), nika.id, 2, "youtube", "yt-5", "Ника", evidence, confirmation="confirmed"
        )
        assert (await social.identify(A, "youtube", "yt-5")).id == nika.id

        with pytest.raises(ContractError) as merge:  # account belongs to somebody else
            await social.link_account(
                command(A),
                nika.id,
                3,
                "youtube",
                "acct-9",
                "Ника",
                evidence,
                confirmation="confirmed",
            )
        assert code(merge) == ErrorCode.INVALID_ARGUMENTS
        assert (await social.identify(A, "youtube", "acct-9")).id == rival.id

        with pytest.raises(ContractError) as stale:
            await social.link_account(
                command(A),
                nika.id,
                1,
                "youtube",
                "yt-6",
                "Ника",
                evidence,
                confirmation="confirmed",
            )
        assert code(stale) == ErrorCode.VERSION_CONFLICT

        await social.link_account(
            command(A), nika.id, 3, "youtube", "yt-5", "Ника", evidence, confirmation="rejected"
        )
        assert await social.identify(A, "youtube", "yt-5") is None  # a wrong link is undone
        with pytest.raises(ContractError):  # the person's only identity cannot be rejected
            await social.link_account(
                command(A),
                nika.id,
                4,
                "twitch",
                "acct-1",
                "Ника",
                evidence,
                confirmation="rejected",
            )
        await world.store.close()

    run(scenario())


def test_returning_person_is_recognised_after_a_restart_without_a_new_introduction(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        person, evidence = await world.person(A, "acct-1", "twitch", "Ника")
        first = await world.social.record_encounter(
            command(A), [person.id], [evidence], "session-1"
        )
        assert first.first_meeting and first.previous_meetings == 0 and not first.duplicate
        # The same utterance seen again (voice + chat, redelivery) is not a new meeting.
        echo = await world.social.record_encounter(command(A), [person.id], [evidence], "session-1")
        assert echo.duplicate and not echo.first_meeting

        world.clock.advance(days=2)
        later = world.restarted()
        found = await later.observe_account(command(A), "twitch", "acct-1", "Ника", evidence)
        assert found.person.id == person.id and not found.created
        second = await later.record_encounter(command(A), [person.id], [evidence], "session-2")
        assert not second.first_meeting and second.previous_meetings == 1
        context = await later.context_for(A, person.id)
        assert context.returning and context.meetings == 2
        assert context.first_met < context.last_met
        await world.store.close()

    run(scenario())


def test_relationships_are_per_persona_versioned_and_idempotent(tmp_path):
    async def scenario():
        from ai_agent_application.memory import EpisodeDraft
        from ai_agent_application.social import AspectLimit, SocialPolicy

        world = await new_world(
            tmp_path,
            [A, B],
            policy=SocialPolicy(aspect_limits=(AspectLimit("trust", 0.0, 1.0, 0.05, 0.05),)),
        )
        world.social.admitted = lambda: True
        a_person, evidence = await world.person(A, "acct-1")
        b_person, b_evidence = await world.person(B, "acct-1")
        await world.memory.record_episode(
            command(A),
            EpisodeDraft(
                sources=(evidence,),
                participant_ids=(a_person.id,),
                choice="Встретилась.",
                actual_outcome="Состоялся разговор.",
                operation_id="relationship-experience",
                provenance="observation",
            ),
        )
        defaults = {"trust": 0.5, "warmth": 0.5, "tension": 0.0}
        social = world.social
        rel = await social.ensure_relationship(command(A), a_person.id, defaults)
        assert rel.version == 1 and social.aspects_of(rel).values == defaults
        assert (await social.ensure_relationship(command(A), a_person.id, defaults)).id == rel.id

        await social.apply_aspects(
            command(A),
            a_person.id,
            1,
            {"trust": 0.55},
            [evidence],
            "change-1",
            bounds={"trust": (0.0, 1.0)},
        )
        updated = await social.get_relationship(A, a_person.id)
        assert updated.version == 2 and social.aspects_of(updated).values["trust"] == 0.55
        assert (
            updated.change_ids == ["change-1"]
            and updated.grounds[-1].source_id == evidence.source_id
        )
        # Replaying the same change does not apply it twice.
        again = await social.apply_aspects(
            command(A), a_person.id, 1, {"trust": 0.55}, [evidence], "change-1"
        )
        assert again.version == 2
        assert (await social.get_relationship(A, a_person.id)).version == 2

        for bad in (
            dict(values={"loyalty": 0.1}),  # aspect not in the profile schema
            dict(values={"trust": "high"}),  # wrong kind
            dict(values={"trust": 1.5}, bounds={"trust": (0.0, 1.0)}),
            dict(values={"trust": 0.6}, grounds=[]),  # no grounds
        ):
            with pytest.raises(ContractError):
                await social.apply_aspects(
                    command(A),
                    a_person.id,
                    2,
                    bad["values"],
                    bad.get("grounds", [evidence]),
                    "change-bad",
                    bounds=bad.get("bounds"),
                )
        with pytest.raises(ContractError) as stale:
            await social.apply_aspects(
                command(A), a_person.id, 1, {"trust": 0.7}, [evidence], "c-9"
            )
        assert code(stale) == ErrorCode.VERSION_CONFLICT and stale.value.error.current_version == 2

        # The other persona has its own, untouched relationship state.
        other = await social.ensure_relationship(command(B), b_person.id, defaults)
        assert social.aspects_of(other).values["trust"] == 0.5 and other.scope == B
        assert b_evidence.scope == B
        await world.store.close()

    run(scenario())


def test_only_the_social_owner_runs_this_module_and_strangers_read_nothing(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        person, _ = await world.person(A)
        for principal in (MEMORY, OPERATOR, WORKER):
            with pytest.raises(ContractError) as error:
                SocialService(world.store, principal, world.registry)
            assert code(error) == ErrorCode.ACCESS_DENIED
        from ai_agent_contracts.memory import Person

        # Operator has a grant but no per-record ACL: the person record stays invisible.
        assert await world.store.repository(Person, OPERATOR, A).get(person.id) is None
        await world.store.close()

    run(scenario())
