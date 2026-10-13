"""The laboratory scenario catalogue on real SQLite with the mock model (M3-04)."""

import pytest
from ai_agent_application.laboratory import catalogue
from lab_support import new_env, run

SCENARIOS = {s.scenario_id: s for s in catalogue()}


def explain(report):
    return [f"{f.name}: {f.detail}" for f in report.failed if f.hard] + [report.error]


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_every_catalogue_scenario_passes_its_hard_invariants(tmp_path, scenario_id):
    async def scenario():
        env = await new_env(tmp_path)
        return await env.runner.run(SCENARIOS[scenario_id])

    report = run(scenario())
    assert report.status == "passed", explain(report)
    names = {f.name for f in report.findings}
    assert {
        "TEST_SCOPE_ONLY",
        "NO_UNSCRIPTED_MODEL_CALLS",
        "STOP_RESPECTED",
        "ISOLATION:work",
        "NO_SECRETS_IN_REPORT",
    } <= names
    assert report.mock and "not LLM quality" in report.as_dict()["note"]


def test_the_catalogue_covers_the_doc22_scenes_and_the_operational_cases():
    assert len(SCENARIOS) == 12
    tags = {tag for s in SCENARIOS.values() for tag in s.tags}
    assert {"doc22", "stop", "failure", "restart", "deletion", "security"} <= tags
    assert sum("doc22" in s.tags for s in SCENARIOS.values()) == 8


def test_the_only_known_gap_is_reported_as_a_soft_finding_not_hidden(tmp_path):
    async def scenario():
        env = await new_env(tmp_path)
        return await env.runner.run(SCENARIOS["quarrel-then-help"])

    report = run(scenario())
    soft = [f for f in report.failed]
    assert report.status == "passed" and [f.hard for f in soft] == [False]
    assert "rate limit" in soft[0].detail


def test_a_whole_run_of_the_catalogue_never_touches_the_work_scope(tmp_path):
    async def scenario():
        env = await new_env(tmp_path)
        before = await _fingerprint(env)
        reports = [await env.runner.run(s) for s in SCENARIOS.values()]
        return before, await _fingerprint(env), reports

    before, after, reports = run(scenario())
    assert before == after and all(r.status == "passed" for r in reports)


async def _fingerprint(env):
    from ai_agent_application.laboratory import Lab, VirtualClock  # noqa: F401
    from ai_agent_application.laboratory.lab import MEMORY
    from ai_agent_contracts.memory import Source

    rows = await env.store.repository(Source, MEMORY, env.work).list(limit=200)
    return [(r.id, r.version) for r in rows]
