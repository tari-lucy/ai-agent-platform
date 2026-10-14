"""M5-03: the synthetic end-to-end cycle, mock model only, real SQLite files, no network.

entry -> dialogue -> restart -> re-meeting -> development -> deletion -> STOP, in ONE world.
The core's entry and STOP run over the real ASGI app; the middle runs the real owners through the
laboratory with a scripted model. Nothing here calls a paid API or publishes anything.
"""

import json
from types import SimpleNamespace

import httpx
from ai_agent_adapters.configuration import BootstrapSettings
from ai_agent_adapters.retention import SQLiteDeletionLedger, SQLitePhysicalStore
from ai_agent_application.development import Candidate
from ai_agent_application.laboratory import Scenario
from ai_agent_application.laboratory.catalogue import FAVORITE, NIKA, prompt, reply
from ai_agent_application.laboratory.lab import LABORATORY, MEMORY, Lab
from ai_agent_application.retention import RetentionService
from ai_agent_contracts.workspace import LaboratoryReport
from e2e_support import new_env, recording_indexes, residue, run
from pydantic import SecretStr

from ai_agent_core.access import password_hash
from ai_agent_core.bootstrap import create_app

SECRET_BIRD = "зимородка-4417"  # unique text the deletion step must remove from every file
FAKE_KEY = "sk-test-0123456789abcdef"  # looks like a key; must never reach a report or a log
ORIGIN = "http://127.0.0.1:8765"
PASSWORD = "synthetic-cycle-password"


def cycle(world: dict):
    """The one scenario. ``world`` carries the paths the physical checks need."""

    async def body(lab: Lab) -> None:
        # ---- dialogue: first meeting, a question, the answer is remembered -----------------
        first = await lab.say(
            NIKA, "Привет! Меня зовут Ника.", reply("Привет, Ника!", question=FAVORITE)
        )
        assert first is not None and first.draft is not None and first.person_id is not None
        lab.check("question asked on first meeting", first.draft.question_topic == "favorite-game")
        answer = await lab.source("Ника: чаще всего играю в шахматы.", author="acct-nika")
        await lab.social.record_answer(
            lab.command(),
            first.person_id,
            "favorite-game",
            "Любимая игра Ники — шахматы.",
            [answer],
        )
        # untrusted text that looks like a key is data: it must not leak into the report
        await lab.say(NIKA, f"Мой ключ {FAKE_KEY}, запомни.", reply("Ключи я не храню."))
        lab.check(
            "key-like text stays out of the model's trusted part",
            FAKE_KEY not in prompt(lab).split("\n", 1)[0],
        )

        # ---- restart: nothing runs until the operator resumes -------------------------------
        await lab.restart()
        calls = len(lab.brain.calls)
        silent = await lab.say(NIKA, "Ты тут?")
        lab.check("after restart nothing runs", silent is None and len(lab.brain.calls) == calls)
        lab.resume()

        # ---- re-meeting: the same person, the known answer, no repeated question -------------
        again = await lab.say(
            NIKA,
            "Я снова здесь!",
            reply("С возвращением!", question={**FAVORITE, "text": "Так во что?"}),
        )
        assert again is not None and again.draft is not None
        lab.check("same person recognised", again.person_id == first.person_id)
        lab.check("returning, not a stranger", 'returning="true"' in prompt(lab))
        lab.check("known answer reaches the model", "шахматы" in prompt(lab))
        lab.check("question not repeated", again.draft.question_topic is None)

        # ---- development: one experience is not enough, two are -----------------------------
        one_episode = await lab.episode(
            "Мы вместе посмеялись над неудачной шуткой.", ["person-nika"]
        )
        state = await lab.persona.get_state(lab.scope)
        assert state is not None

        def candidate(episodes: tuple[str, ...], name: str) -> Candidate:
            return Candidate(
                proposal_id=name,
                property="humor",
                new_value=0.62,
                interpretation="Шутки сближают.",
                alternative="Возможно, настроение.",
                uncertainty=0.3,
                expected_manifestation="Чаще шутит.",
                episode_ids=episodes,
                base_version=state.version,
            )

        rejected = await lab.development.submit(lab.scope, candidate((one_episode,), "one"))
        lab.check("one experience is not enough", rejected.change.status.value == "REJECTED")
        two_episode = await lab.episode("Ещё раз вместе смеялись над шуткой.", ["person-nika"])
        ready = await lab.development.submit(
            lab.scope, candidate((one_episode, two_episode), "two")
        )
        applied = await lab.development.apply(lab.scope, ready.change.id)
        lab.check(
            "two experiences apply with evidence",
            applied.status.value == "APPLIED" and bool(applied.evidence),
        )
        await lab.say(NIKA, "Расскажи шутку!", reply("Ладно, слушай."))
        lab.check("next situation is built on the changed state", "humor: 0.62" in prompt(lab))

        # ---- deletion: gone from the model's view AND from every file ------------------------
        ref = await lab.source(
            f"Ника рассказала про редкую птицу {SECRET_BIRD}.", author="acct-nika"
        )
        await lab.say(NIKA, "Помнишь про птицу?", reply("Да, помню."))
        lab.check("remembered before deletion", SECRET_BIRD in prompt(lab))
        lab.check(
            "the text is on disk before deletion", bool(residue(world["files"](lab), SECRET_BIRD))
        )
        physical = SQLitePhysicalStore(lab.storage)
        ledger = SQLiteDeletionLedger(world["dir"] / f"{lab.scope.memory_space_id}.ledger.sqlite")
        retention = RetentionService(
            lab.storage,
            MEMORY,
            lab.memory,
            physical,
            ledger,
            index=lab.memory.index,
            clock=lab.clock,
        )
        preview = await retention.preview_delete(lab.scope, [ref.source_id])
        report = await retention.delete(lab.scope, preview)
        lab.check("deletion completed", report.complete)
        await lab.say(NIKA, "А про птицу что скажешь?", reply("Не помню подробностей."))
        lab.check("deleted text does not return to the model", SECRET_BIRD not in prompt(lab))
        lab.check("deleted text is on no file", residue(world["files"](lab), SECRET_BIRD) == [])
        ledger.close()

        # ---- STOP in the middle of a generation ----------------------------------------------
        assert lab.controller is not None
        original = lab.controller.stream.emit  # type: ignore[union-attr]

        async def emit(event_id: str, text: str) -> None:
            await original(event_id, text)
            if len(lab.stream_text.get(event_id, "")) > 8:
                lab.stop()

        lab.controller.stream = SimpleNamespace(emit=emit, close=lab.controller.stream.close)  # type: ignore[union-attr]
        stopped = await lab.say(
            None,
            "Расскажи длинную историю.",
            reply(
                "Это очень длинная история, которая не должна дойти до конца.",
                question=FAVORITE,
                promise={
                    "promise_key": "p",
                    "content": "Расскажу.",
                    "completion_criterion": "Рассказано",
                    "topic_key": "t",
                },
            ),
        )
        assert stopped is not None
        lab.check("output closed by STOP", stopped.status == "cancelled" and stopped.draft is None)
        lab.check("no action proposed, nothing published", not lab.proposals)
        lab.check(
            "a promise is not recorded from a cut generation",
            not await lab.social.obligations(lab.scope),
        )
        calls = len(lab.brain.calls)
        lab.check(
            "after STOP nothing runs",
            await lab.say(NIKA, "Ты слышишь?") is None and len(lab.brain.calls) == calls,
        )

    return body


def build(tmp_path):
    async def make():
        env = await new_env(tmp_path)
        indexes = recording_indexes(env)
        world = {"dir": env.base}

        def files(lab):
            return [lab.storage.engine.url.database, *indexes]

        world["files"] = files
        scenario = Scenario("synthetic-cycle", 1, "Сквозной цикл", cycle(world), ("m5-03",))
        return env, scenario

    return make


def test_the_whole_cycle_passes_in_one_world_and_replays(tmp_path):
    async def go():
        env, scenario = await build(tmp_path)()
        first = await env.runner.run(scenario, seed="cycle-1")
        again = await env.runner.replay(scenario, first)
        repo = env.store.repository(LaboratoryReport, LABORATORY, env.runner.scopes[0])
        return first, again, await repo.list(limit=10)

    report, replayed, stored = run(go())
    failed = [f.name for f in report.findings if not f.ok]
    assert report.status == "passed" and not failed, failed
    assert replayed, "same seed in a fresh world must give the same digest"
    names = {f.name for f in report.findings}
    assert {
        "TEST_SCOPE_ONLY",
        "NO_UNSCRIPTED_MODEL_CALLS",
        "STOP_RESPECTED",
        "NO_SECRETS_IN_REPORT",
    } <= names
    assert any(name.startswith("ISOLATION:") for name in names)
    assert (
        len(stored) == 1
        and stored[0].status == "passed"
        and stored[0].summary.startswith("MOCK passed")
    )
    assert FAKE_KEY not in repr(report) and FAKE_KEY not in json.dumps(report.log, default=str)
    assert SECRET_BIRD not in json.dumps(report.log, default=str)


def test_a_different_seed_gives_a_different_digest_but_the_same_verdict(tmp_path):
    async def go():
        env, scenario = await build(tmp_path)()
        one = await env.runner.run(scenario, seed="cycle-a", persist=False)
        two = await env.runner.run(scenario, seed="cycle-b", persist=False)
        return one, two

    one, two = run(go())
    assert one.status == two.status == "passed" and one.digest != two.digest


def test_the_work_biography_is_never_touched_by_the_cycle(tmp_path):
    async def go():
        env, scenario = await build(tmp_path)()
        before = await _work_text(env)
        await env.runner.run(scenario, seed="cycle-iso", persist=False)
        return before, await _work_text(env)

    before, after = run(go())
    assert before and before == after


async def _work_text(env):
    from ai_agent_contracts.memory import Source

    repo = env.store.repository(Source, MEMORY, env.work)
    return [s.content for s in await repo.list(limit=50)]


def test_entry_stop_and_exit_over_the_real_core_leak_nothing(tmp_path):
    async def go():
        settings = BootstrapSettings(
            workspace_id="synthetic",
            persona_id="alpha",
            memory_scope="test",
            operator_origin=ORIGIN,
            operator_password_hash=password_hash(SecretStr(PASSWORD)),
        )
        app = create_app(settings)
        seen = []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as client:
            login = await client.post(
                "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
            )
            seen.append(login)
            csrf, scope = login.json()["csrf_token"], login.json()["scopes"][0]
            query = {
                k: scope[k]
                for k in ("workspace_id", "persona_id", "memory_space_id", "memory_mode")
            }
            sessions = await client.get("/api/v1/sessions", params=query)
            seen.append(sessions)
            row = sessions.json()["items"][0]
            assert (
                row["state"] == "STOPPED"
            )  # a fresh core starts closed; entry never starts the agent
            command = {
                "type": "agent.stop",
                "request_id": "cycle-stop",
                "scope": row["scope"],
                "target": row["agent_id"],
            }
            headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
            stop = await client.post(
                f"/api/v1/agents/{row['agent_id']}/stop", json=command, headers=headers
            )
            twice = await client.post(
                f"/api/v1/agents/{row['agent_id']}/stop", json=command, headers=headers
            )
            seen += [stop, twice]
            business = await client.post(
                "/api/v1/commands",
                json={
                    "request_id": "msg",
                    "type": "dialogue.message",
                    "target": row["agent_id"],
                    "scope": row["scope"],
                    "arguments": {"text": "привет"},
                },
                headers=headers,
            )
            seen.append(business)
            logout = await client.post("/api/v1/auth/logout", json={}, headers=headers)
            after = await client.get("/api/v1/auth/session")
        return login, stop, twice, business, logout, after, seen, app

    login, stop, twice, business, logout, after, seen, app = run(go())
    assert login.status_code == 200 and "httponly" in login.headers["set-cookie"].lower()
    assert "samesite=strict" in login.headers["set-cookie"].lower()
    assert stop.status_code == 200 and stop.json()["status"] == "completed"
    assert (
        twice.status_code == 200 and twice.json() == stop.json()
    )  # a repeated STOP changes nothing
    assert business.status_code == 503  # the business path is closed until the owners are composed
    assert logout.status_code == 204 and after.status_code == 401
    for response in seen:
        assert response.status_code < 500 or response is business
        text = response.text + " ".join(
            f"{k}={v}" for k, v in response.headers.items() if k.lower() != "set-cookie"
        )
        assert PASSWORD not in text and "sk-" not in text
    assert app.state.bootstrap_manifest["effects_enabled"] is False
