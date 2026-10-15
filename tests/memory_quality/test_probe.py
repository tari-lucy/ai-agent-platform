"""The opt-in probe's harness, run against a synthetic transport: it never calls a real API."""

import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from quality_support import Model
from test_live import binding

from ai_agent_core.local_host import provider as providers

PATH = Path(__file__).resolve().parents[2] / "scripts/probes/private_memory_quality.py"
CONSENT = "--i-understand-this-makes-paid-api-calls"


def load():
    spec = importlib.util.spec_from_file_location("private_memory_quality_probe", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_probe_refuses_to_start_without_explicit_consent_limits_and_absolute_paths(
    tmp_path, capsys
):
    probe = load()
    base = ["--model", "m", "--env-file", str(tmp_path / "e"), "--workdir", str(tmp_path / "w")]
    for argv in (
        base,  # no consent
        [CONSENT, *base, "--max-calls", "7"],
        [CONSENT, *base, "--max-calls", "3"],
        [CONSENT, "--model", "m", "--env-file", "rel.env", "--workdir", str(tmp_path / "w")],
        [CONSENT, "--model", "m", "--env-file", str(tmp_path / "e"), "--workdir", "rel"],
    ):
        with pytest.raises(SystemExit) as stop:
            probe.parse(argv)
        assert stop.value.code == 2
    assert probe.parse([CONSENT, *base]).max_calls == 6
    capsys.readouterr()


def test_the_wire_ceiling_stops_the_call_after_the_limit_without_sending_it():
    probe = load()
    sent = []

    async def inner(request):
        sent.append(request)
        return httpx.Response(200)

    async def scenario():
        wire = probe.Counting(httpx.MockTransport(inner), 1)
        async with httpx.AsyncClient(transport=wire) as client:
            body = {"messages": [{"role": "system", "content": "x"}]}
            await client.post("https://provider.invalid/v1/chat", json=body)
            with pytest.raises(httpx.TransportError):
                await client.post("https://provider.invalid/v1/chat", json=body)
        return wire.calls

    calls = asyncio.run(scenario())
    assert len(sent) == 1 and [c["role"] for c in calls] == ["character"]


def test_a_six_call_run_reports_counts_and_codes_without_any_text(tmp_path, monkeypatch):
    probe = load()
    monkeypatch.setattr(providers, "OpenAICompatibleProvider", providers.OpenAICompatibleProvider)
    model = Model()
    model.queues["character"] += [dict(reply=f"Ответ {i}.") for i in range(3)]
    model.queues["extractor"].append(dict(proposals=[]))
    model.queues["consolidator"].append(
        dict(content="По словам Ники, ей нравятся синие зонтики; встреча у маяка в пятницу.")
    )
    model.queues["reflection"].append(dict(keep_state_reason="Мало опыта.", changes=[]))
    arguments = probe.parse(
        [
            CONSENT,
            "--model",
            "fixture-model",
            "--env-file",
            str(binding(tmp_path / "synthetic.env")),
            "--workdir",
            str(tmp_path / "host"),
        ]
    )
    original_provider = providers.OpenAICompatibleProvider
    report = asyncio.run(probe.run(arguments, httpx.MockTransport(model)))
    assert providers.OpenAICompatibleProvider is original_provider
    text = json.dumps(report, ensure_ascii=False, default=str)
    assert [c["role"] for c in report["calls"]] == [
        "character",
        "character",
        "extractor",
        "consolidator",
        "reflection",
        "character",
    ]
    assert report["steps"]["cycle"] == "SUCCEEDED" and report["steps"]["character_unchanged"]
    assert any("outcome=model_returned_nothing" in line for line in report["pipeline_log"])
    final = report["final_request"]
    assert final["memory_mentions_umbrellas"] and final["memory_mentions_lighthouse"]
    assert [m["by"] for m in final["derived_memory"]] == ["сводка-интерпретация"]
    # the usage the scripted wire did not report stays unknown, never zero-complete
    assert report["usage"]["attempts"] == 6
    assert {c["read_timeout_seconds"] for c in report["calls"]} == {60.0}
    for forbidden in ("Мне нравятся", "provider.invalid", "fixture-$literal", "Мало опыта"):
        assert forbidden not in text


def test_probe_stops_after_the_first_transport_failure(tmp_path, monkeypatch):
    probe = load()
    sent = []

    async def broken(request):
        sent.append(True)
        raise httpx.ReadTimeout("synthetic fixed failure")

    arguments = probe.parse(
        [
            CONSENT,
            "--model",
            "fixture-model",
            "--env-file",
            str(binding(tmp_path / "synthetic.env")),
            "--workdir",
            str(tmp_path / "host"),
            "--max-calls",
            "4",
        ]
    )
    original_provider = providers.OpenAICompatibleProvider
    report = asyncio.run(probe.run(arguments, httpx.MockTransport(broken)))
    assert providers.OpenAICompatibleProvider is original_provider
    assert len(sent) == 1 and len(report["calls"]) == 1
    assert report["steps"] == {"reply_1": "FAILED"}
    assert not report["cycle"]
