"""The sweep keeps verbatim excerpts before the full text expires; deletion and rights follow."""

from ai_agent_contracts.memory import Claim
from retention_support import MEMORY, command, make_scope, new_world, residue, run
from test_retention import claim, hits, stored

A = make_scope("persona-a")
TALK = (
    "Сначала долго обсуждали погоду. Мой талисман — бирюзовый фонарик бабушки. "
    "Секретный-код-Пурпур нигде не нужен. Потом говорили о чае."
)


def test_the_sweep_keeps_an_excerpt_and_the_claim_outlives_the_conversation(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        talk = await world.add(A, TALK, external_id="t-1")  # one day old: 29 days left
        lone = await world.add(A, "Реплика без выводов.", external_id="t-2")
        await claim(world, A, "claim-lamp", [talk], text="Её талисман — бирюзовый фонарик.")
        world.clock.advance(days=20)  # outside the 7-day lead: nothing is kept yet
        early = await world.retention.sweep(A)
        before = await world.memory.get_source(A, world.memory.excerpt_id(A, talk.source_id))
        world.clock.advance(days=5)  # inside the lead: the excerpt is prepared
        await world.retention.sweep(A)
        excerpt = await world.memory.get_source(A, world.memory.excerpt_id(A, talk.source_id))
        world.clock.advance(days=5)  # the full conversation expires and is erased
        late = await world.retention.sweep(A)
        lamp = await world.store.repository(Claim, MEMORY, A).get("claim-lamp")
        found = [f.content for f in await hits(world, A, "бирюзовый фонарик")]
        raw = await stored(world, A, talk.source_id)
        return dict(
            early=early,
            before=before,
            excerpt=excerpt,
            late=late,
            lamp=lamp,
            found=found,
            raw=raw,
            talk=talk,
            lone=lone,
            world=world,
        )

    out = run(scenario())
    assert out["early"].expired == () and out["before"] is None
    excerpt = out["excerpt"]
    assert excerpt.content == "Мой талисман — бирюзовый фонарик бабушки."
    assert set(out["late"].expired) == {out["talk"].source_id, out["lone"].source_id}
    assert out["late"].unconsolidated == (out["lone"].source_id,)  # kept one is not "lost"
    assert out["raw"].availability == "expired" and not out["raw"].has_content
    assert out["lamp"] is not None and [r.source_id for r in out["lamp"].sources] == [excerpt.id]
    assert out["found"] == [excerpt.content]  # recall finds the excerpt, not the raw text
    assert residue(out["world"].paths, "Секретный-код-Пурпур") == []  # the rest is gone
    run(out["world"].store.close())


def test_deleting_or_closing_the_conversation_takes_its_excerpt_along(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        talk = await world.add(A, TALK, external_id="t-1")
        other = await world.add(A, "Её любимая птица — серая цапля.", external_id="t-3")
        await claim(world, A, "claim-lamp", [talk], text="Её талисман — бирюзовый фонарик.")
        await claim(world, A, "claim-bird", [other], text="Её любимая птица — серая цапля.")
        world.clock.advance(days=25)
        await world.retention.sweep(A)  # both excerpts exist, both talks still alive
        bird = world.memory.excerpt_id(A, other.source_id)
        current = await world.memory.get_source(A, other.source_id)
        await world.memory.set_rights(
            command(A),
            other.source_id,
            current.version,
            read_allowed=True,
            api_transfer_allowed=False,
            public_disclosure_allowed=False,
        )
        followed = await world.memory.get_source(A, bird)
        preview = await world.retention.preview_delete(A, [talk.source_id])
        done = await world.retention.delete(A, preview)
        lamp_excerpt = world.memory.excerpt_id(A, talk.source_id)
        gone = await stored(world, A, lamp_excerpt)
        lamp = await world.store.repository(Claim, MEMORY, A).get("claim-lamp")
        return dict(
            followed=followed,
            preview=preview,
            done=done,
            gone=gone,
            lamp=lamp,
            lamp_excerpt=lamp_excerpt,
            world=world,
        )

    out = run(scenario())
    assert out["followed"].api_transfer_allowed is False  # the excerpt follows revoked rights
    assert out["lamp_excerpt"] in out["preview"].source_ids  # the operator sees it in the plan
    assert out["done"].complete and out["gone"].availability == "deleted"
    assert not out["gone"].has_content and out["lamp"] is None
    assert residue(out["world"].paths, "бирюзовый фонарик бабушки") == []
    run(out["world"].store.close())
