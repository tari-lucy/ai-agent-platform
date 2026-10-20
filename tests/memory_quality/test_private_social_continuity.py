"""The private interlocutor is one stable person, and a confirmed promise is exact memory.

Production-built host, the public profile, real HTTP owners and the live adapter over a synthetic
scripted transport. Identity comes from the authenticated panel login, never from a name in the
text. What the model SAW is asserted from its request body; scripted answers prove the route.
"""

import asyncio
import re
from types import SimpleNamespace

from ai_agent_application.social import SocialService
from ai_agent_application.social.keys import person_id_for
from ai_agent_application.storage import Owner
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.memory import Encounter, Person, Promise
from quality_support import Model, prepared_host
from test_http import finish_job, post

from ai_agent_core.local_host.init import test_scope as lab_scope
from ai_agent_core.m3_composition.dialogue import PANEL_ACCOUNT, PANEL_PLATFORM, memory
from ai_agent_core.m3_composition.state import PRINCIPALS, command

BOOKS = dict(
    promise_key="books-list",
    content="Прислать Нике список вымышленных книг о маяках.",
    completion_criterion="Список книг отправлен в разговоре.",
    topic_key="books",
)
TEA = dict(
    promise_key="tea-recipe",
    content="Рассказать рецепт чая с чабрецом.",
    completion_criterion="Рецепт рассказан.",
    topic_key="tea",
)
OBLIGATION = re.compile(r"<obligation[^>]*>(.*?)</obligation>", re.S)
PERSON = re.compile(r'<person name="([^"]*)" returning="(\w+)" meetings="(\d+)"')


def seen(body: dict) -> dict:
    user = body["messages"][-1]["content"]
    return dict(obligations=OBLIGATION.findall(user), person=PERSON.findall(user))


async def unconfirmed(host, request_id, text, reply, promise):
    """A reply with a promise that the operator never confirms."""
    host.model.queues["character"].append(dict(reply=reply, promise=promise))
    run = host.runtime.supervisor.snapshot
    made = await post(
        host.env,
        dict(
            type="dialogue.message",
            request_id=request_id,
            target=run.session_id,
            scope=run.scope.model_dump(mode="json"),
            arguments={"text": text},
        ),
    )
    assert made.status_code == 200, made.text
    await finish_job(host.env, made.json()["operation_id"])


async def promised(host, request_id, text, reply, promise):
    return await host.say(request_id, text, reply, promise=promise)


async def close_command(host, request_id, promise, outcome, result):
    """The panel's promise.close owner command over HTTP."""
    data = {"promise_id": promise.id, "outcome": outcome}
    if result is not None:
        data["closure_result"] = result
    return await post(
        host.env,
        dict(
            type="promise.close",
            request_id=request_id,
            target=promise.id,
            scope=host.scope.model_dump(mode="json"),
            expected_version=promise.version,
            arguments=dict(schema_id="promise.close-args", data=data),
        ),
    )


def social(rt):
    return SocialService(rt.store, PRINCIPALS[Owner.SOCIAL], rt.registry)


async def records(rt, scope, model):
    return await rt.store.repository(model, PRINCIPALS[Owner.SOCIAL], scope).list(limit=200)


def test_same_person_after_restart_exact_promises_and_their_history(tmp_path, monkeypatch):
    async def scenario():
        model = Model()
        host = await prepared_host(tmp_path, model, monkeypatch)
        out: dict = {}
        async with host.open():
            await host.start()
            rt, scope = host.runtime, host.scope
            # the name in the text is data: identity is the authenticated login
            await promised(
                host,
                "m1",
                "Меня зовут Ника. Пришлёшь список книг о маяках?",
                "Пришлю список книг о маяках.",
                BOOKS,
            )
            await unconfirmed(host, "m2", "А рецепт чая?", "Расскажу рецепт чая.", TEA)
            out["promises"] = [(p.content, p.status) for p in await records(rt, scope, Promise)]
            # another account with the same display name is another person
            other_ref = SourceRef(
                source_id=(await host.sources())["Меня зовут Ника. Пришлёшь список книг о маяках?"],
                version=1,
                scope=scope,
                availability="available",
            )
            other = await social(rt).observe_account(
                command(scope, "other-person"), "fixture-chat", "nika", "оператор", other_ref
            )
            assert other is not None
            out["other"] = other.person.id
            for n in range(6):  # unrelated talk: the promise is exact, not a search hit
                await host.say(f"f{n}", f"Вымышленная реплика {n} о погоде.", f"Ответ {n}.")
            out["before_restart"] = seen(model.last("character"))
        async with host.open():  # restart: the same login is the same person
            await host.start()
            rt, scope = host.runtime, host.scope
            await host.say("after", "Я снова здесь. Что ты мне обещала?", "Список книг.")
            out["after_restart"] = seen(model.last("character"))
            out["people"] = sorted(p.id for p in await records(rt, scope, Person))
            out["meetings"] = len(await records(rt, scope, Encounter))
            # withdrawal is a new version; the old one stays in history
            books = next(p for p in await records(rt, scope, Promise))
            out["panel_sees"] = [p["id"] for p in await host.get("promises")]
            bad = await close_command(host, "bad-close", books, "fulfilled", None)
            out["bad_close"] = bad.status_code
            done = await close_command(
                host, "withdraw-books", books, "withdrawn", "Оператор отменил просьбу."
            )
            assert done.status_code == 200, done.text
            closed = await social(rt).obligations(scope)  # nothing owed any more
            closed = SimpleNamespace(version=done.json()["version"], owed=closed)
            await host.say("withdrawn", "Что ты мне обещала?", "Пока ничего.")
            out["after_withdraw"] = seen(model.last("character"))
            repo = rt.store.repository(Promise, PRINCIPALS[Owner.SOCIAL], scope)
            out["history"] = [
                (await repo.get(books.id, version=v)).status for v in (1, closed.version)
            ]
            # a promise whose evidence is no longer transferable stays out of the request
            tea = await promised(
                host, "m3", "Расскажешь про чай с чабрецом?", "Расскажу про чабрец.", TEA
            )
            tea_promise = next(p for p in await records(rt, scope, Promise) if p.id != books.id)
            evidence = tea_promise.sources[0]
            await memory(rt).set_rights(  # the source was registered in this run
                command(rt.supervisor.snapshot.scope, "revoke-tea"),
                evidence.source_id,
                evidence.version,
                read_allowed=True,
                api_transfer_allowed=False,
                public_disclosure_allowed=False,
            )
            await host.say("revoked", "Что ты мне обещала?", "Пока ничего.")
            out["after_revoke"] = seen(model.last("character"))
            out["tea_utterance"] = tea.id == tea_promise.delivered_utterance_id
            lab = lab_scope(scope, host.config.test_space_ids[0])
            out["lab"] = await social(rt).obligations(lab)
            out["scope"] = scope
        return out

    out = asyncio.run(scenario())
    operator = person_id_for(out["scope"], PANEL_PLATFORM, PANEL_ACCOUNT)
    # only the confirmed reply made a promise; the unconfirmed one is not "made"
    assert out["promises"] == [(BOOKS["content"], "open")]
    before = out["before_restart"]
    assert before["obligations"] == [
        BOOKS["content"] + " | критерий: " + BOOKS["completion_criterion"]
    ]
    assert out["other"] != operator and sorted([out["other"], operator]) == out["people"]
    after = out["after_restart"]
    assert after["obligations"] == before["obligations"]
    assert after["person"] == [("оператор", "true", str(out["meetings"]))]
    assert out["meetings"] >= 2  # one per session of the same person
    assert out["after_withdraw"]["obligations"] == []
    assert out["history"] == ["open", "withdrawn"]  # the change kept its history
    assert out["bad_close"] >= 400  # "fulfilled" without the operator's stated result
    assert len(out["panel_sees"]) == 1  # the operator reviews the promise in the panel
    assert out["tea_utterance"] and out["after_revoke"]["obligations"] == []
    assert out["lab"] == []  # TEST memory has no work promises


async def as_older_host(root):
    """A host prepared before SOCIAL rights: old matrix, flag off."""
    from ai_agent_adapters.storage.database import SQLiteStore

    from ai_agent_core.local_host import rights
    from ai_agent_core.local_host.init import work_scope
    from ai_agent_core.local_host.layout import load_config, paths, save_config

    host = paths(root)
    config = load_config(host)
    store = SQLiteStore(host.database)
    try:
        await store.provision(
            work_scope(config.workspace_id, config.persona_id, config.work_space_id),
            tuple(
                g
                for g in rights.work_grants(operator_controls=config.operator_controls)
                if Owner.SOCIAL not in (g.owner, g.principal.owner)
            ),
        )
    finally:
        await store.close()
    save_config(host, config.model_copy(update={"private_social": False}))


def test_an_older_host_stays_anonymous_until_social_is_enabled_offline(tmp_path, monkeypatch):
    from ai_agent_core.local_host.social import enable_social

    async def scenario():
        model = Model()
        host = await prepared_host(tmp_path, model, monkeypatch)
        await as_older_host(host.root)
        out: dict = {}
        async with host.open():
            await host.start()
            await promised(host, "old", "Пришлёшь список книг?", "Пришлю список.", BOOKS)
            rt, scope = host.runtime, host.scope
            out["before"] = (
                len(await records(rt, scope, Person)),
                len(await records(rt, scope, Promise)),
            )
        await enable_social(host.root)
        async with host.open():
            await host.start()
            await promised(host, "new", "Пришлёшь список книг?", "Пришлю список.", BOOKS)
            rt, scope = host.runtime, host.scope
            out["after"] = (
                [p.id for p in await records(rt, scope, Person)],
                [p.status for p in await records(rt, scope, Promise)],
            )
            out["scope"] = scope
        return out

    out = asyncio.run(scenario())
    assert out["before"] == (0, 0)  # the reply still worked, anonymously, without new rights
    operator = person_id_for(out["scope"], PANEL_PLATFORM, PANEL_ACCOUNT)
    assert out["after"] == ([operator], ["open"])


def test_the_operator_marks_a_promise_fulfilled_with_their_own_stated_result(tmp_path, monkeypatch):
    from ai_agent_contracts.memory import Source

    async def scenario():
        model = Model()
        host = await prepared_host(tmp_path, model, monkeypatch)
        async with host.open():
            await host.start()
            await promised(host, "m1", "Пришлёшь список книг?", "Пришлю список.", BOOKS)
            rt, scope = host.runtime, host.scope
            books = (await records(rt, scope, Promise))[0]
            done = await close_command(
                host, "books-done", books, "fulfilled", "Список книг получен в чате."
            )
            assert done.status_code == 200, done.text
            replay = await close_command(
                host, "books-done", books, "fulfilled", "Список книг получен в чате."
            )
            assert replay.status_code == 200 and replay.json() == done.json()
            changed = await close_command(host, "books-done", books, "withdrawn", "Подмена.")
            assert changed.status_code == 409
            again = await close_command(host, "books-again", books, "withdrawn", "Повтор.")
            closed = (await records(rt, scope, Promise))[0]
            proof = await rt.store.repository(Source, PRINCIPALS[Owner.MEMORY], scope).get(
                closed.sources[-1].source_id
            )
            await host.say("after", "Что ты мне обещала?", "Ничего не осталось.")
            return dict(
                status=closed.status,
                result=closed.closure_result,
                proof=(proof.source_type, proof.provenance.value, proof.content),
                again=again.status_code,
                owed=seen(model.last("character"))["obligations"],
            )

    out = asyncio.run(scenario())
    assert out["status"] == "fulfilled" and out["result"] == "Список книг получен в чате."
    assert out["proof"][0] == "promise-closure" and out["proof"][2] == out["result"]
    assert out["again"] >= 400  # a stale version cannot reopen or rewrite it
    assert out["owed"] == []


def test_deleting_the_first_message_keeps_the_interlocutor_and_the_dialogue(tmp_path, monkeypatch):
    from ai_agent_contracts.memory import Source

    async def scenario():
        model = Model()
        host = await prepared_host(tmp_path, model, monkeypatch)
        async with host.open():
            await host.start()
            rt, scope = host.runtime, host.scope
            await host.say("m1", "Первое вымышленное сообщение.", "Ответ.")
            await host.say("m2", "Второе сообщение.", "Ответ.")
            first = next(
                s
                for s in await rt.store.repository(Source, PRINCIPALS[Owner.MEMORY], scope).list(
                    limit=200
                )
                if s.content == "Первое вымышленное сообщение."
            )
            await memory(rt).delete_source(
                command(first.scope, "delete-first"), first.id, first.version
            )
            await host.say("m3", "Ты меня помнишь?", "Да.")  # used to fail: hidden person
            people = await records(rt, scope, Person)
            return seen(model.last("character")), [p.id for p in people], scope

    view, people, scope = asyncio.run(scenario())
    operator = person_id_for(scope, PANEL_PLATFORM, PANEL_ACCOUNT)
    assert people == [operator]  # one person, grounded by the login, not by a message
    # still the identified interlocutor; the meeting grounded by the deleted message is
    # forgotten with it, as deletion requires
    assert [row[0] for row in view["person"]] == ["оператор"]
