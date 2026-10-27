"""Private live wiring over accepted owners; only synthetic HTTPX transport, no paid API."""

import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from ai_agent_adapters.llm import ADAPTER_ID, OpenAICompatibleProvider
from ai_agent_application.laboratory.profile import REFERENCE_PROFILE
from ai_agent_contracts.states import OperationState

from ai_agent_core.local_host import provider as providers
from ai_agent_core.local_host.init import initialise
from ai_agent_core.local_host.layout import HostError, LiveConfig, load_config, paths
from ai_agent_core.local_host.provider import provider_factory
from ai_agent_core.local_host.serve import build


def binding(path, endpoint="https://provider.invalid/v1", credential="fixture-$literal"):
    path.write_text(
        f"AI_PLATFORM_LLM_ENDPOINT='{endpoint}'\nAI_PLATFORM_LLM_API_KEY='{credential}'\n",
        encoding="utf-8",
    )
    return path


def test_live_requires_explicit_preparation_and_binding(tmp_path):
    live = LiveConfig(model_id="fixture-model")
    cases = [
        (dict(mode="live"), "LIVE_NOT_PREPARED"),
        (dict(mode="live", live=live), "ABSOLUTE_ENV_FILE_REQUIRED"),
        (dict(mode="live", live=live, env_file=Path("relative")), "ABSOLUTE_ENV_FILE_REQUIRED"),
        (dict(mode="mock", live=live), "PROVIDER_MODE_MISMATCH"),
        (dict(mode="mock", env_file=tmp_path / "not-read"), "PROVIDER_MODE_MISMATCH"),
    ]
    for arguments, code in cases:
        with pytest.raises(HostError, match=code):
            provider_factory(**arguments)
    env = tmp_path / "binding.env"
    binding(env, endpoint="https://provider.invalid/v1/messages")
    with pytest.raises(HostError, match="LLM_BASE_PATH_REQUIRED"):
        provider_factory("live", live=live, env_file=env)
    binding(env, endpoint="http://localhost/v1")
    with pytest.raises(HostError, match="LLM_HTTPS_REQUIRED"):
        provider_factory("live", live=live, env_file=env)
    binding(env, credential="")
    with pytest.raises(HostError, match="LLM_BINDING_REQUIRED"):
        provider_factory("live", live=live, env_file=env)
    binding(env, endpoint="https://provider.invalid")
    factory = provider_factory("live", live=live, env_file=env)
    assert factory(ADAPTER_ID).provider._settings["AI_PLATFORM_LLM_ENDPOINT"].endswith("/v1")


def sse(payload):
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def answer(content='{"reply":"Вымышленный ответ."}', *, finish="stop", usage=True):
    text = sse({"model": "fixture-model", "choices": [{"delta": {"content": content}}]})
    text += sse({"choices": [{"delta": {}, "finish_reason": finish}]})
    if usage:
        text += sse(
            {
                "choices": [],
                "usage": {"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38},
            }
        )
    return (text + "data: [DONE]\n\n").encode()


@pytest.mark.parametrize("period", ["lifetime", "utc_day"])
def test_real_adapter_private_http_ledger_errors_stop_and_durable_budget(
    tmp_path, monkeypatch, caplog, period
):
    async def scenario():
        root = tmp_path / "host"
        live = LiveConfig(model_id="fixture-model", attempt_limit=6, attempt_period=period)
        config = await initialise(root, profile_markdown=REFERENCE_PROFILE, live=live)
        env = binding(tmp_path / "binding.env")
        calls = []
        mode = "ok"
        entered, released = asyncio.Event(), asyncio.Event()
        closed = []

        class Paused(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                yield sse({"choices": [{"delta": {"content": '{"reply":"поздний'}}]}).encode()
                await released.wait()
                yield answer('ответ"}')

            async def aclose(self):
                closed.append(True)

        async def send(request):
            assert str(request.url) == "https://provider.invalid/v1/chat/completions"
            assert request.headers["authorization"] == "Bearer fixture-$literal"
            body = json.loads(request.content)
            assert body["model"] == live.model_id and body["max_tokens"] == 512
            assert body["stream_options"] == {"include_usage": True}
            assert "tools" not in body and "temperature" not in body and "top_p" not in body
            assert body["messages"][0]["role"] == "system"
            assert body["messages"][0]["content"].endswith(providers.PRIVATE_JSON_PROTOCOL)
            assert "response_format" not in body  # Gateway does not document this capability
            assert "fixture-$literal" not in request.content.decode()
            calls.append(mode)
            if mode in {"auth", "rate"}:
                return httpx.Response(
                    401 if mode == "auth" else 429, json={"error": "fixture-REMOTE-SECRET"}
                )
            if mode == "pause":
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=Paused()
                )
            data = answer('```json\n{"reply":"Вымышленный ответ."}\n```')
            if mode == "bad-json":
                data = answer("invalid-json")
            elif mode == "truncated":
                data = answer(finish="length", usage=False)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)

        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as wire:
            monkeypatch.setattr(
                providers,
                "OpenAICompatibleProvider",
                lambda settings: OpenAICompatibleProvider(settings, wire),
            )
            password = paths(root).password.read_text().strip()
            base = dict(
                workspace_id=config.workspace_id,
                persona_id=config.persona_id,
                memory_space_id=config.work_space_id,
                memory_mode="work",
            )
            origin = "http://127.0.0.1:8765"

            async def session(app, *, restart=False):
                nonlocal mode
                rt = app.state.runtime
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url=origin
                ) as http:
                    assert (
                        await http.get("/api/v1/private-api-status", params=base)
                    ).status_code == 401
                    login = await http.post(
                        "/api/v1/auth/login",
                        json={"password": password},
                        headers={"Origin": origin},
                    )
                    assert login.status_code == 200
                    headers = {"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]}
                    assert rt.supervisor.snapshot.state.value == "STOPPED"
                    run = rt.supervisor.snapshot
                    started = await http.post(
                        "/api/v1/commands",
                        headers=headers,
                        json=dict(
                            type="agent.start",
                            request_id="restart" if restart else "start",
                            target="primary",
                            scope=run.scope.model_dump(mode="json"),
                            expected_version=run.version,
                            arguments={"autonomy": "autonomous"},
                        ),
                    )
                    assert started.status_code == 200, started.text

                    async def message(identifier, success=False, wait=True):
                        run = rt.supervisor.snapshot
                        sent = await http.post(
                            "/api/v1/commands",
                            headers=headers,
                            json=dict(
                                type="dialogue.message",
                                request_id=identifier,
                                target=run.session_id,
                                scope=run.scope.model_dump(mode="json"),
                                arguments={"text": identifier},
                            ),
                        )
                        assert sent.status_code == 200, sent.text
                        operation = sent.json()["operation_id"]
                        if not wait:
                            return operation
                        for _ in range(400):
                            job, _ = await rt.jobs.store.load(operation, run.scope)
                            if job.status in {
                                OperationState.SUCCEEDED,
                                OperationState.FAILED,
                                OperationState.CANCELLED,
                                OperationState.OUTCOME_UNKNOWN,
                            }:
                                if success:
                                    assert job.status == OperationState.SUCCEEDED, job.status
                                return job
                            await asyncio.sleep(0.025)
                        raise AssertionError("job did not finish")

                    if restart:
                        await message("cap-after-restart")
                        assert len(calls) == 6
                        attempts = (await http.get("/api/v1/llm-attempts", params=base)).json()[
                            "items"
                        ]
                        assert len(attempts) == 6
                        status = await http.get("/api/v1/private-api-status", params=base)
                        assert status.status_code == 200, status.text
                        value = status.json()
                        assert value["attempts_used"] == 6 and value["attempts_remaining"] == 0
                        assert value["period"] == period
                        assert (value["resets_at"] is not None) == (period == "utc_day")
                        foreign = await http.get(
                            "/api/v1/private-api-status", params={**base, "persona_id": "foreign"}
                        )
                        assert foreign.status_code == 403
                        return

                    await message("first", success=True)
                    await message("first", success=True)  # idempotent replay cannot spend again
                    assert calls == ["ok"]
                    replies = (await http.get("/api/v1/utterances", params=base)).json()["items"]
                    assert len(replies) == 1 and replies[0]["moderation_status"] == "draft"
                    attempts = (await http.get("/api/v1/llm-attempts", params=base)).json()["items"]
                    assert len(attempts) == 1 and attempts[0]["usage"]["input_tokens"] == 30
                    assert attempts[0]["tariff_id"] is None
                    ack = await http.post(
                        "/api/v1/commands",
                        headers=headers,
                        json=dict(
                            type="dialogue.acknowledge",
                            request_id="ack",
                            target=replies[0]["id"],
                            scope=rt.supervisor.snapshot.scope.model_dump(mode="json"),
                            expected_version=replies[0]["version"],
                            arguments={"revision": replies[0]["revision"]},
                        ),
                    )
                    assert ack.status_code == 200
                    for mode in ("bad-json", "auth", "rate", "truncated"):
                        await message(mode)
                    assert calls == ["ok", "bad-json", "auth", "rate", "truncated"]
                    replies = (await http.get("/api/v1/utterances", params=base)).json()["items"]
                    assert len(replies) == 1  # broken/truncated/error text never becomes a final
                    attempts = (await http.get("/api/v1/llm-attempts", params=base)).json()["items"]
                    assert len(attempts) == 5 and "fixture-REMOTE-SECRET" not in json.dumps(
                        attempts
                    )
                    truncated = next(a for a in attempts if a["usage"]["source"] == "unknown")
                    assert truncated["usage"]["input_tokens"] is None

                    # An oversized prompt is refused before a provider call or ledger reservation.
                    budget = rt.private_api_budget
                    rt.private_api_budget = replace(budget, max_prompt_chars=1000)
                    with caplog.at_level(logging.WARNING):
                        await message("context-over-limit")
                    assert len(calls) == 5
                    # The exact reason with sizes only, not the generic CAPABILITY_UNAVAILABLE.
                    refused = [
                        r for r in caplog.records if "refused before the provider" in r.message
                    ]
                    assert refused and "MODEL_PROMPT_LIMIT" in refused[-1].message
                    assert "limit=1000" in refused[-1].message
                    assert "context-over-limit" not in caplog.text  # no prompt content
                    rt.private_api_budget = budget
                    mode = "pause"
                    await message("late-stop", wait=False)
                    await asyncio.wait_for(entered.wait(), timeout=10)
                    stopped = await http.post(
                        "/api/v1/agents/primary/stop",
                        headers=headers,
                        json=dict(
                            type="agent.stop",
                            request_id="stop",
                            target="primary",
                            scope=rt.supervisor.snapshot.scope.model_dump(mode="json"),
                        ),
                    )
                    assert stopped.status_code == 200
                    released.set()
                    for _ in range(200):
                        if rt.supervisor.snapshot.state.value == "STOPPED" and closed:
                            break
                        await asyncio.sleep(0.025)
                    assert closed and rt.supervisor.snapshot.state.value == "STOPPED"
                    replies = (await http.get("/api/v1/utterances", params=base)).json()["items"]
                    assert len(replies) == 1
                    attempts = (await http.get("/api/v1/llm-attempts", params=base)).json()["items"]
                    assert len(attempts) == 6
                    assert any(a["status"] == "cancelled" for a in attempts)

            for restart in (False, True):
                app, close = build(
                    paths(root), load_config(paths(root)), provider="live", env_file=env
                )
                try:
                    async with app.router.lifespan_context(app):
                        await session(app, restart=restart)
                finally:
                    close()

    asyncio.run(scenario())
    assert "Private API provider failure: AUTH" in caplog.text
    assert "Private API provider failure: RATE_LIMITED" in caplog.text
    assert "fixture-REMOTE-SECRET" not in caplog.text and "fixture-$literal" not in caplog.text
    assert "provider.invalid" not in caplog.text


def test_live_provider_refuses_foreign_roles_models_and_unbounded_parameters(tmp_path):
    from ai_agent_application.llm import ChatMessage, ProviderCall, ProviderError
    from ai_agent_application.llm.model import GenerationParameters, ResolvedAssignment

    live = LiveConfig(model_id="fixture-model", max_prompt_chars=24000)
    factory = provider_factory("live", live=live, env_file=binding(tmp_path / "binding.env"))
    provider = factory(ADAPTER_ID)
    assignment = ResolvedAssignment(
        assignment_id="asg-character",
        assignment_version=1,
        role_id="character",
        connection_id="conn-live",
        adapter_id=ADAPTER_ID,
        model_id="fixture-model",
        endpoint_setting="AI_PLATFORM_LLM_ENDPOINT",
        credential_setting="AI_PLATFORM_LLM_API_KEY",
        parameters=GenerationParameters(max_output_tokens=512, timeout_seconds=60),
        capability_verification="unverified",
    )
    for changes in (
        {"role_id": "reflection"},
        {"model_id": "another-model"},
        {"connection_id": "conn-other"},
        {"credential_setting": "FOREIGN_KEY"},
        {"parameters": GenerationParameters(max_output_tokens=1024)},
        {"parameters": GenerationParameters(max_output_tokens=512, timeout_seconds=120)},
    ):
        with pytest.raises(ProviderError, match="NOT_ADMITTED"):
            provider.stream(
                ProviderCall(
                    assignment=replace(assignment, **changes),
                    messages=(ChatMessage("system", "synthetic"),),
                )
            )
    with pytest.raises(ProviderError, match="NOT_ADMITTED"):
        provider.stream(
            ProviderCall(assignment=assignment, messages=(ChatMessage("system", "x" * 24001),))
        )


def test_private_json_envelope_keeps_closed_schema_and_partial_results_blocked():
    from ai_agent_application.dialogue.model import DialogueDecision
    from ai_agent_application.llm.service import parse_result

    good = '{"reply":"Вымышленная реплика."}'
    assert providers.private_reply_text("```json\n" + good + "\n```") == good
    assert providers.private_reply_text("```\n" + good + "\n```") == good
    assert providers.private_reply_text(good) == good
    for text in (
        "Вступление\n```json\n" + good + "\n```",
        "```json\n" + good + "\n```\nпослесловие",
        "```json\n" + good,
        "```python\n" + good + "\n```",
        '```json\n{"reply":"x","execute":"unregistered"}\n```',
        '```json\n{"reply":"x","reply":"duplicate"}\n```',
        '```json\n{"reply":"x","question":{"text":"incomplete"}}\n```',
        "```json\n" + good + "\n```\n```json\n" + good + "\n```",
    ):
        normalised = providers.private_reply_text(text)
        assert normalised == text
        assert parse_result(normalised, DialogueDecision) is None
