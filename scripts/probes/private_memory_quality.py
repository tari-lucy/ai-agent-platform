"""Opt-in paid probe: one synthetic talk through the real private memory path (max 6 calls).

Builds a NEW temporary host with the public example profile, talks to the configured model
through the real adapter (two replies), runs one memory cycle (extractor, consolidator,
reflection) and, if the call budget allows, asks one follow-up that should see that memory.
It never reads the env file itself (the host's normal loader does), uses no working biography
and installs nothing. The report holds counts, fixed codes and booleans: no prompt, answer,
dialogue text, endpoint or key. Usage that the provider did not report stays "unknown", not 0.

Run it only after review, never from pytest. See scripts/probes/README.md.
"""

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
from ai_agent_adapters.llm import OpenAICompatibleProvider
from ai_agent_application.storage import Owner
from ai_agent_application.usage import UsageLedger
from ai_agent_contracts.control import Utterance
from ai_agent_contracts.memory import Claim, MemoryCycle, Summary
from ai_agent_contracts.persona import CharacterState, ReflectionCheckpoint, StateChange

from ai_agent_core.local_host import provider as providers
from ai_agent_core.local_host.init import initialise, work_scope
from ai_agent_core.local_host.layout import HostError, LiveConfig, load_config, paths
from ai_agent_core.local_host.memory import enable_memory
from ai_agent_core.local_host.serve import build
from ai_agent_core.m3_composition.state import PRINCIPALS

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profiles/examples/opechatka/profile.md"
ORIGIN = "http://127.0.0.1:8765"
CONSENT = "--i-understand-this-makes-paid-api-calls"
MARKERS = (
    ("Ты извлекаешь проверяемые утверждения", "extractor"),
    ("Кратко перескажи разговор", "consolidator"),
    ("осмыслить пережитое", "reflection"),
)
TURN = re.compile(r'<turn role="(?:person|character)"')
MEMORY = re.compile(r'<memory by="([^"]+)"[^>]*?( partial="true")?>(.*?)</memory>', re.S)
WATCHED = (
    "ai_agent_application.extraction.extractor",
    "ai_agent_application.extraction.consolidator",
    "ai_agent_core.m3_composition.long_term",
    "ai_agent_core.m3_composition.memory_cycles",
    "ai_agent_core.local_host.provider",
)
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "OUTCOME_UNKNOWN", "EXPIRED"}
FIRST = (
    "Привет! Меня зовут Ника. Мне нравятся синие зонтики, "
    "и давай договоримся встретиться у маяка в пятницу."
)
SECOND = "Расскажи коротко, чем ты сегодня занималась в редакции?"
LAST = "Напомни, какие зонтики мне нравятся и о чём мы договорились на пятницу?"
FICTION = "Это вымышленный тестовый разговор. "


class Collector(logging.Handler):
    """Keeps only the fixed one-line messages of the watched loggers."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name in WATCHED:
            self.lines.append(record.getMessage())


class Counting(httpx.AsyncBaseTransport):
    """Hard call ceiling at the wire; keeps counts and shapes, never bodies."""

    def __init__(self, inner: httpx.AsyncBaseTransport, limit: int) -> None:
        self.inner, self.limit = inner, limit
        self.calls: list[dict] = []
        self.final_view: dict | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if len(self.calls) >= self.limit:
            raise httpx.TransportError("CALL_LIMIT")
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        role = next((r for m, r in MARKERS if m in system), "character")
        entry = {"n": len(self.calls) + 1, "role": role, "prompt_chars": len(request.content)}
        self.calls.append(entry)
        if role == "character":
            user = body["messages"][-1]["content"]
            fragments = MEMORY.findall(user)
            derived = [f for f in fragments if f[0].startswith(("сводка-", "утверждение-"))]
            self.final_view = dict(
                turns=len(TURN.findall(user)),
                recalled=len(fragments) - len(derived),
                derived_memory=[dict(by=by, partial=bool(partial)) for by, partial, _ in derived],
                memory_mentions_umbrellas=any("зонтик" in t.lower() for *_, t in derived),
                memory_mentions_lighthouse=any("маяк" in t.lower() for *_, t in derived),
            )
        response = await self.inner.handle_async_request(request)
        entry["http_status"] = response.status_code
        return response


async def poll(env, operation_id: str, seconds: int = 240) -> str:
    scope = env.runtime.supervisor.snapshot.scope
    for _ in range(seconds * 10):
        stored = await env.runtime.jobs.store.load(operation_id, scope)
        if stored is not None and stored[0].status.value in TERMINAL:
            return stored[0].status.value
        await asyncio.sleep(0.1)
    return "TIMEOUT"


async def command(env, **body) -> httpx.Response:
    return await env.client.post("/api/v1/commands", json=body, headers=env.headers)


async def say(env, request_id: str, text: str) -> str:
    run = env.runtime.supervisor.snapshot
    scope = run.scope.model_dump(mode="json")
    made = await command(
        env,
        type="dialogue.message",
        request_id=request_id,
        target=run.session_id,
        scope=scope,
        arguments={"text": text},
    )
    if made.status_code != 200:
        return f"HTTP_{made.status_code}"
    status = await poll(env, made.json()["operation_id"])
    if status != "SUCCEEDED":
        return status
    rows = await env.runtime.store.repository(Utterance, PRINCIPALS[Owner.GATEWAY], run.scope).list(
        limit=200
    )
    reply = next(u for u in rows if u.operation_id == made.json()["operation_id"])
    ack = await command(
        env,
        type="dialogue.acknowledge",
        request_id="ack-" + request_id,
        target=reply.id,
        scope=scope,
        expected_version=reply.version,
        arguments={"revision": reply.revision},
    )
    return "SUCCEEDED" if ack.status_code == 200 else f"ACK_HTTP_{ack.status_code}"


async def observe(env, scope, wire: Counting, collector: Collector, steps: dict) -> dict:
    store = env.runtime.store
    claims = await store.repository(Claim, PRINCIPALS[Owner.MEMORY], scope).list(limit=200)
    cycles = await store.repository(MemoryCycle, PRINCIPALS[Owner.MEMORY], scope).list(limit=200)
    attempts = await UsageLedger(store, PRINCIPALS[Owner.LLM], env.runtime.registry).attempts(scope)
    complete = [a for a in attempts if a.usage.completeness == "complete"]
    changes = await store.repository(StateChange, PRINCIPALS[Owner.PERSONA], scope).list(limit=200)
    checkpoints = await store.repository(
        ReflectionCheckpoint, PRINCIPALS[Owner.PERSONA], scope
    ).list(limit=200)
    return dict(
        steps=steps,
        calls=wire.calls,
        cycle=[dict(stage=c.stage, status=c.status, error=c.error_code) for c in cycles],
        claims=dict(
            total=len(claims),
            by_type={
                t: sum(c.knowledge_type == t for c in claims)
                for t in sorted({c.knowledge_type for c in claims})
            },
        ),
        summaries=len(
            await store.repository(Summary, PRINCIPALS[Owner.MEMORY], scope).list(limit=200)
        ),
        reflection=dict(checkpoints=len(checkpoints), proposed_changes=len(changes)),
        pipeline_log=collector.lines,
        final_request=wire.final_view,
        usage=dict(
            attempts=len(attempts),
            complete=len(complete),
            unknown_or_partial=len(attempts) - len(complete),
            input_tokens_complete=sum(a.usage.input_tokens or 0 for a in complete),
            output_tokens_complete=sum(a.usage.output_tokens or 0 for a in complete),
        ),
    )


async def run(arguments: argparse.Namespace, inner: httpx.AsyncBaseTransport | None = None) -> dict:
    root = arguments.workdir
    live = LiveConfig(model_id=arguments.model, attempt_limit=20, attempt_period="utc_day")
    await initialise(
        root,
        profile_markdown=PROFILE.read_text(encoding="utf-8"),
        operator_controls=True,
        live=live,
    )
    await enable_memory(root)
    config = load_config(paths(root))
    scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
    collector, steps = Collector(), {}
    logging.getLogger().addHandler(collector)
    logging.getLogger().setLevel(logging.INFO)
    try:
        return await converse(arguments, inner, config, scope, collector, steps)
    finally:
        logging.getLogger().removeHandler(collector)


async def converse(arguments, inner, config, scope, collector, steps) -> dict:
    root = arguments.workdir
    async with httpx.AsyncClient(
        transport=(wire := Counting(inner or httpx.AsyncHTTPTransport(), arguments.max_calls))
    ) as client:
        providers.OpenAICompatibleProvider = lambda settings: OpenAICompatibleProvider(  # type: ignore[misc]
            settings, client
        )
        app, close = build(paths(root), config, provider="live", env_file=arguments.env_file)
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url=ORIGIN
                ) as http:
                    login = await http.post(
                        "/api/v1/auth/login",
                        json={"password": paths(root).password.read_text().strip()},
                        headers={"Origin": ORIGIN},
                    )
                    env = SimpleNamespace(
                        runtime=app.state.runtime,
                        client=http,
                        headers={"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]},
                    )
                    run_state = env.runtime.supervisor.snapshot
                    await command(
                        env,
                        type="agent.start",
                        request_id="probe-start",
                        target="primary",
                        scope=run_state.scope.model_dump(mode="json"),
                        expected_version=run_state.version,
                        arguments={"autonomy": "autonomous"},
                    )
                    first = (FICTION if arguments.label_fictional else "") + FIRST
                    steps["reply_1"] = await say(env, "probe-1", first)
                    steps["reply_2"] = await say(env, "probe-2", SECOND)
                    items = (
                        await http.get(
                            "/api/v1/episodes",
                            params={
                                "workspace_id": scope.workspace_id,
                                "persona_id": scope.persona_id,
                                "memory_space_id": scope.memory_space_id,
                                "memory_mode": scope.memory_mode,
                            },
                        )
                    ).json()["items"]
                    steps["episodes"] = len(items)
                    state = env.runtime.supervisor.snapshot
                    before = await env.runtime.store.repository(
                        CharacterState, PRINCIPALS[Owner.PERSONA], scope
                    ).list(limit=10)
                    cycle = await command(
                        env,
                        type="session.process-memory",
                        request_id="probe-cycle",
                        target=state.session_id,
                        scope=state.scope.model_dump(mode="json"),
                        expected_version=state.version,
                        arguments={"episode_ids": [e["id"] for e in items]},
                    )
                    steps["cycle"] = (
                        await poll(env, cycle.json()["operation_id"])
                        if cycle.status_code == 200
                        else f"HTTP_{cycle.status_code}"
                    )
                    after = await env.runtime.store.repository(
                        CharacterState, PRINCIPALS[Owner.PERSONA], scope
                    ).list(limit=10)
                    steps["character_unchanged"] = before == after
                    if arguments.max_calls >= 6 and steps["cycle"] == "SUCCEEDED":
                        steps["reply_3"] = await say(env, "probe-3", LAST)
                    return await observe(env, scope, wire, collector, steps)
        finally:
            close()


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(CONSENT, dest="consent", action="store_true", required=True)
    parser.add_argument("--model", required=True, help="model id the configured endpoint serves")
    parser.add_argument("--env-file", type=Path, required=True, help="absolute path to local .env")
    parser.add_argument("--workdir", type=Path, required=True, help="NEW absolute directory")
    parser.add_argument("--max-calls", type=int, default=6, help="5 (no follow-up) or 6")
    parser.add_argument("--label-fictional", action="store_true", help="say the talk is fictional")
    arguments = parser.parse_args(argv)
    if arguments.max_calls not in (5, 6):
        parser.error("--max-calls must be 5 or 6")
    for name in ("env_file", "workdir"):
        if not getattr(arguments, name).is_absolute():
            parser.error(f"--{name.replace('_', '-')} must be absolute")
    return arguments


def main(argv: list[str] | None = None) -> int:
    arguments = parse(sys.argv[1:] if argv is None else argv)
    try:
        report = asyncio.run(run(arguments))
    except HostError as error:
        print(json.dumps({"error": error.code}))
        return 2
    except Exception as error:  # fixed class name only: messages may carry paths or URLs
        print(json.dumps({"error": type(error).__name__}))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
