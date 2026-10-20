"""Verbatim retained excerpts keep claims/summaries grounded after the full text expires."""

from datetime import timedelta

from ai_agent_application.memory import ClaimDraft
from ai_agent_application.memory.service import EXCERPT_CHARS, EXCERPT_POLICY, EXCERPT_TYPE
from ai_agent_contracts.api import Receipt
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.memory import Claim, Source, Summary
from memory_support import MEMORY, NOW, command, make_scope, new_world, run

A = make_scope("persona-a")
TERM = timedelta(days=365)
RAW = (
    "Сегодня обсуждали погоду и порт. Мой талисман — бирюзовый фонарик бабушки. "
    "Потом говорили про чай с бергамотом. Ещё немного о газете."
)


def ref(made, scope=A):
    return SourceRef(
        source_id=made.source_id, version=made.version, scope=scope, availability="available"
    )


async def summary(world, scope, name, text, refs):
    now = world.clock()
    record = Summary(
        id="summary-" + name,
        scope=scope,
        created_at=now,
        updated_at=now,
        version=1,
        period_start=now - timedelta(hours=1),
        period_end=now,
        episode_ids=[],
        sources=refs,
        content=text,
        analysis_id="fixture-analysis",
    )
    request = command(scope)
    async with world.store.transaction(MEMORY, scope, request) as unit:
        await unit.put(record)
        await unit.complete(
            Receipt(
                request_id=request.request_id,
                scope=scope,
                status="completed",
                resource_id=record.id,
                version=1,
            )
        )


def test_an_excerpt_keeps_the_relevant_words_and_moves_the_records_onto_it(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        raw = await world.add(A, RAW, external_id="talk-1", api_transfer_allowed=False)
        other = await world.add(
            A, "Другой разговор о маяке.", external_id="talk-2", occurred_at=NOW
        )
        await world.memory.record_claim(
            command(A),
            ClaimDraft(
                "claim-lamp",
                "person-1",
                "Её талисман — бирюзовый фонарик.",
                "reported",
                (ref(raw), ref(other)),
                0.6,
            ),
        )
        await summary(world, A, "day", "По словам собеседницы, талисман — фонарик.", [ref(raw)])
        dependents = (("Claim", "claim-lamp"), ("Summary", "summary-day"), ("Promise", "p-1"))
        kept = await world.memory.retain(command(A), raw.source_id, dependents, term=TERM)
        again = await world.memory.retain(command(A), raw.source_id, dependents, term=TERM)
        excerpt = await world.memory.get_source(A, kept)
        claims = world.store.repository(Claim, MEMORY, A)
        lamp = await claims.get("claim-lamp")
        day = await world.store.repository(Summary, MEMORY, A).get("summary-day")
        nothing = await world.memory.retain(command(A), other.source_id, (), term=TERM)
        twice = await world.memory.retain(command(A), kept, (("Claim", "claim-lamp"),), term=TERM)
        world.clock.advance(days=29, hours=12)  # past the first talk's 30 days, not the second
        after = (
            await world.memory.get_source(A, raw.source_id),
            await claims.get("claim-lamp"),
            await world.store.repository(Summary, MEMORY, A).get("summary-day"),
        )
        world.clock.advance(days=400)  # and the excerpt is past its own term
        late = await claims.get("claim-lamp")
        await world.store.close()
        return dict(
            kept=kept,
            again=again,
            excerpt=excerpt,
            lamp=lamp,
            day=day,
            raw=raw,
            other=other,
            nothing=nothing,
            twice=twice,
            after=after,
            late=late,
        )

    out = run(scenario())
    excerpt: Source = out["excerpt"]
    assert out["kept"] == out["again"]  # idempotent
    assert excerpt.content == "Мой талисман — бирюзовый фонарик бабушки."  # verbatim, relevant
    assert excerpt.content in RAW and len(excerpt.content) <= EXCERPT_CHARS
    assert (excerpt.source_type, excerpt.retention.policy_id) == (EXCERPT_TYPE, EXCERPT_POLICY)
    assert excerpt.author_id == "person-1" and not excerpt.api_transfer_allowed  # rights kept
    assert excerpt.occurred_at == NOW - timedelta(days=1)
    assert excerpt.retention.expires_at == NOW - timedelta(days=1) + TERM
    lamp, day = out["lamp"], out["day"]
    assert lamp.version == 2 and [r.source_id for r in lamp.sources] == [
        out["kept"],
        out["other"].source_id,
    ]
    assert day.version == 2 and [r.source_id for r in day.sources] == [out["kept"]]
    assert out["nothing"] is None and out["twice"] is None  # no records / never re-excerpted
    raw_after, lamp_after, day_after = out["after"]
    assert raw_after is None  # the full text is gone
    assert lamp_after is not None and day_after is not None  # the knowledge stays grounded
    assert out["late"] is None  # nothing lives forever


def test_a_long_conversation_keeps_only_a_bounded_excerpt(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        filler = " ".join(f"Фонарик упоминался в реплике номер {n}." for n in range(60))
        raw = await world.add(A, filler, external_id="long")
        await world.memory.record_claim(
            command(A),
            ClaimDraft("claim-f", "person-1", "Упоминался фонарик.", "reported", (ref(raw),), 0.6),
        )
        kept = await world.memory.retain(
            command(A), raw.source_id, (("Claim", "claim-f"),), term=TERM
        )
        excerpt = await world.memory.get_source(A, kept)
        await world.store.close()
        return excerpt, filler

    excerpt, filler = run(scenario())
    assert len(excerpt.content) <= EXCERPT_CHARS and excerpt.content.startswith("Фонарик")
    assert len(excerpt.content) < len(filler) / 3  # space is saved
