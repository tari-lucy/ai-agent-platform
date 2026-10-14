"""M5-04: the harness over the real laboratory, dry-run stand-in, budget guard and real-run gate.

Everything here is a mock: no network, no key, no paid call. A stand-in that *reports provider
usage* plays the part of a paid model so the ceiling logic can be exercised.
"""

import dataclasses
import json
import socket
from decimal import Decimal

import pytest
from ai_agent_application.evaluation import (
    Authorisation,
    BudgetGuard,
    Evaluation,
    Price,
    RealRunRefused,
    quality_catalogue,
)
from ai_agent_application.evaluation.checks import (
    failed_without_output,
    never_in_prompts,
    no_reply,
    seen_in_prompts,
)
from ai_agent_application.evaluation.model import Call, Say, Trace, Turn
from ai_agent_application.evaluation.providers import DryModel
from ai_agent_application.laboratory.lab import assignment
from ai_agent_application.llm import Failure, ProviderError, Started, UsageReported
from ai_agent_contracts.llm import TokenUsage
from eval_support import new_env, run

PRICE = Price("USD", Decimal("3"), Decimal("15"))


class PaidStandIn(DryModel):
    """Plays a paid model: same canned replies, usage marked as reported by the provider."""

    def __init__(self, input_tokens=1000, output_tokens=100):
        self.usage = TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            source="provider",
            completeness="complete",
        )

    async def _play(self, call):
        async for event in super()._play(call):
            yield UsageReported(self.usage) if isinstance(event, UsageReported) else event


class SilentUsage(DryModel):
    async def _play(self, call):
        async for event in super()._play(call):
            if not isinstance(event, UsageReported):
                yield event


class Broken:
    def stream(self, call):
        async def fail():
            raise ProviderError(Failure.AUTH)
            yield  # pragma: no cover

        return fail()


def authorisation(ceiling="1", **extra):
    return Authorisation(
        real_calls=True, ceiling=Decimal(ceiling), price=PRICE, model_label="paid-stand-in", **extra
    )


def pick(*ids):
    return [s for s in quality_catalogue() if s.scenario_id in ids]


def evaluate(tmp_path, provider=None, auth=None, ids=("Q-01",), repeats=1):
    async def go():
        env = await new_env(tmp_path)
        if provider is None:
            ev = Evaluation.dry(env.runner, price=PRICE)
        else:
            ev = Evaluation.real(env.runner, provider, auth or authorisation(), assignment)
        return ev, await ev.evaluate(pick(*ids), repeats=repeats)

    return run(go())


# ---- dry-run ------------------------------------------------------------------------------


def test_dry_run_of_all_twenty_scenarios_passes_and_is_labelled_mock(tmp_path):
    ev, report = evaluate(tmp_path, ids=[s.scenario_id for s in quality_catalogue()])
    assert [r.status for r in report.runs] == ["passed"] * 20
    assert report.mock and report.mode == "dry-run (mock)" and ev.mock
    data = report.as_dict()
    assert "НЕ ОЦЕНЕНО" in data["quality"] and data["recall"].startswith("не измерено")
    assert report.usage()["usage_sources"] == ["local_estimate"]
    assert report.usage()["unknown_usage_calls"] == 0
    assert all(c["model"] == "lab-mock-character" for r in report.runs for c in r.calls)
    assert "dry-run на заглушке" in report.to_markdown()


def test_dry_run_is_replayable_and_isolated(tmp_path):
    _, first = evaluate(tmp_path / "a", ids=("Q-01", "Q-12"), repeats=2)
    _, second = evaluate(tmp_path / "b", ids=("Q-01", "Q-12"), repeats=2)
    assert [r.digest for r in first.runs] == [r.digest for r in second.runs]
    assert len({r.digest for r in first.runs}) == 4  # different seeds, different worlds


def test_dry_run_never_touches_the_network(tmp_path, monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("network access")

    async def checked():
        # Windows creates an internal socketpair when its event loop starts.
        # Block connects after loop creation, before any evaluation preparation/IO.
        monkeypatch.setattr(socket.socket, "connect", refuse)
        env = await new_env(tmp_path)
        try:
            return await Evaluation.dry(env.runner, price=PRICE).evaluate(
                pick("Q-01", "Q-10"), repeats=1
            )
        finally:
            await env.store.close()

    report = run(checked())
    assert all(r.status == "passed" for r in report.runs)


def test_price_preview_counts_estimates_but_never_stops(tmp_path):
    ev, report = evaluate(tmp_path, ids=("Q-01", "Q-02"))
    assert ev.guard.ceiling is None and not ev.guard.exhausted
    assert ev.guard.spent_provider == 0 and ev.guard.spent_estimate > 0
    assert report.budget["stopped_reason"] is None


def test_provider_fault_is_injected_without_billing_and_leaves_no_half_reply(tmp_path):
    ev, report = evaluate(tmp_path, ids=("Q-10",))
    run_ = report.runs[0]
    assert run_.status == "passed"
    # injected faults never reach the provider: nothing is booked for them
    assert ev.guard.unknown_calls == 0 and ev.guard.calls == len(run_.calls) - 2
    failed = [c for c in run_.calls if c["failure"]]
    assert len(failed) == 2 and all(c["input_tokens"] is None for c in failed)
    cut = next(t for t in run_.turns if t["index"] == 2)
    assert cut["status"] == "failed" and cut["reply"] is None
    assert run_.turns[-1]["reply"]  # the later session is served normally


def test_stop_and_restart_scenarios_get_no_public_output(tmp_path):
    _, report = evaluate(tmp_path, ids=("Q-09", "Q-11"))
    for run_ in report.runs:
        silent = [t for t in run_.turns if t["index"] == 2]
        assert silent[0]["reply"] is None
        assert "STOP_RESPECTED" not in run_.hard_failures


def test_deleted_text_does_not_return_and_was_there_before(tmp_path):
    _, report = evaluate(tmp_path, ids=("Q-12",))
    run_ = report.runs[0]
    assert run_.status == "passed" and run_.soft_failures == []


def test_distinct_accounts_with_one_nickname_stay_two_people_and_merging_is_detected(tmp_path):
    _, report = evaluate(tmp_path, ids=("Q-03",))
    assert report.runs[0].status == "passed"
    scenario = pick("Q-03")[0]
    same = dataclasses.replace(
        scenario, people={**scenario.people, "nika2": scenario.people["nika"]}
    )

    async def go():
        env = await new_env(tmp_path / "merged")
        return await Evaluation.dry(env.runner).evaluate([same], repeats=1)

    merged = run(go()).runs[0]
    assert merged.status == "failed" and "PEOPLE_COUNT=2" in merged.hard_failures


def test_private_note_never_reaches_the_model_and_a_leak_is_detected(tmp_path):
    _, report = evaluate(tmp_path, ids=("Q-19",))
    assert report.runs[0].status == "passed"
    scenario = pick("Q-19")[0]
    leaked = dataclasses.replace(
        scenario,
        steps=tuple(
            dataclasses.replace(s, text=s.text + " Лев задолжал Артёму.")
            if isinstance(s, Say) and s.who == "artem"
            else s
            for s in scenario.steps
        ),
    )

    async def go():
        env = await new_env(tmp_path / "leak")
        return await Evaluation.dry(env.runner).evaluate([leaked], repeats=1)

    bad = run(go()).runs[0]
    assert bad.status == "failed" and "NOT_IN_PROMPTS:private-note" in bad.hard_failures


def test_canon_check_passes_on_injection_text(tmp_path):
    _, report = evaluate(tmp_path, ids=("Q-08", "Q-20"))
    assert [r.status for r in report.runs] == ["passed", "passed"]


def test_key_like_text_is_redacted_in_the_transcript(tmp_path):
    scenario = dataclasses.replace(
        pick("Q-01")[0],
        steps=(Say("nika", "мой ключ sk-test-0123456789abcdef", True), *pick("Q-01")[0].steps),
    )

    async def go():
        env = await new_env(tmp_path)
        return await Evaluation.dry(env.runner).evaluate([scenario], repeats=1)

    report = run(go())
    assert "sk-test-0123456789abcdef" not in report.to_json()
    assert "sk-test-0123456789abcdef" not in report.to_markdown()
    assert report.runs[0].turns[0]["text"] == "[REDACTED]"


# ---- checks on fabricated traces ----------------------------------------------------------


def turn(index, reply=None, status="drafted"):
    return Turn(index, 1, "nika", "t", status, None, reply, (), None, None, (), 0.0)


def finding(check, trace):
    return run(check(None, trace))


def test_checks_fail_when_the_guarantee_is_broken():
    trace = Trace(turns=[turn(0, "привет"), turn(1, None, "failed")])
    assert not finding(no_reply(0), trace)[0].ok
    assert finding(no_reply(1), trace)[0].ok
    assert not finding(failed_without_output(0), trace)[0].ok
    assert finding(failed_without_output(1), trace)[0].ok
    assert not finding(failed_without_output(7), trace)[0].ok  # no such turn


def test_prompt_checks_ignore_the_characters_own_replies_and_respect_the_step_window():
    trace = Trace(
        turns=[
            dataclasses.replace(turn(0, "зимородок красив"), call_ids=(0,)),
            dataclasses.replace(turn(1, "ок"), call_ids=(1,)),
        ],
        calls=[
            Call("character", "m", "ПАМЯТЬ: зимородок"),
            Call("character", "m", "история: зимородок красив\nпросто"),
        ],
    )
    assert finding(never_in_prompts("зимородок", from_step=1), trace)[0].ok  # only own reply
    assert not finding(never_in_prompts("зимородок", from_step=0), trace)[0].ok
    seen = finding(seen_in_prompts("зимородок", until_step=1), trace)[0]
    assert seen.ok and not seen.hard
    missing = finding(seen_in_prompts("зимородок", until_step=0), trace)[0]
    assert not missing.ok and not missing.hard


# ---- budget and the real-run gate ---------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"real_calls": False},
        {"ceiling": Decimal("0")},
        {"ceiling": Decimal("-1")},
        {"model_label": " "},
        {"max_output_tokens": 0},
    ],
)
def test_real_run_is_refused_without_a_complete_authorisation(tmp_path, change):
    auth = dataclasses.replace(authorisation(), **change)

    async def go():
        env = await new_env(tmp_path)
        return Evaluation.real(env.runner, PaidStandIn(), auth, assignment)

    with pytest.raises(RealRunRefused):
        run(go())


def test_real_run_with_reported_usage_is_priced_from_the_provider_and_labelled_real(tmp_path):
    ev, report = evaluate(tmp_path, PaidStandIn(), ids=("Q-01",))
    assert not report.mock and report.mode == "real" and not ev.mock
    assert ev.guard.spent_estimate == 0
    calls = report.usage()["provider_calls"]
    assert ev.guard.spent_provider == calls * Decimal("0.0045")
    assert "dry-run" not in report.to_markdown()
    assert "Ждёт оценок" in report.as_dict()["quality"]


def test_ceiling_stops_the_run_before_it_is_crossed_and_skips_the_rest(tmp_path):
    ev, report = evaluate(
        tmp_path, PaidStandIn(), authorisation("0.05"), ids=("Q-01", "Q-02", "Q-03"), repeats=3
    )
    assert ev.guard.spent <= Decimal("0.05")
    assert ev.guard.reason == "CEILING_WOULD_BE_EXCEEDED" and report.stopped_reason
    skipped = [r for r in report.runs if r.status == "skipped"]
    assert skipped and report.runs[-1].status == "skipped"
    assert all(r.skipped_reason == report.stopped_reason for r in skipped)
    assert report.score_sheet()["scores"][-1]["scenario"] != "Q-03" or len(skipped) < 9


def test_unknown_usage_is_never_priced_as_zero_and_stops_the_run(tmp_path):
    ev, report = evaluate(tmp_path, SilentUsage(), ids=("Q-01", "Q-02"), repeats=2)
    assert ev.guard.unknown_calls == 3 and ev.guard.spent_estimate > 0
    assert ev.guard.reason == "UNKNOWN_USAGE_LIMIT"
    assert report.usage()["unknown_usage_calls"] >= 3
    assert any(r.status == "skipped" for r in report.runs)


def test_provider_failure_before_any_output_is_not_billed(tmp_path):
    ev, report = evaluate(tmp_path, Broken(), ids=("Q-01",))
    assert ev.guard.spent == 0 and ev.guard.calls == 0
    assert report.runs[0].status in ("passed", "failed", "error")
    assert all(c["failure"] == "AUTH" for c in report.runs[0].calls)


def test_guard_unit_rules():
    guard = BudgetGuard(price=PRICE, ceiling=Decimal("0.03"))
    assert guard.admit(300)
    guard.charge(None, 300, 0)  # unknown: books the worst case, never zero
    assert guard.unknown_calls == 1 and guard.spent > 0
    first = guard.spent
    guard.charge(
        TokenUsage(input_tokens=10, output_tokens=5, source="provider", completeness="complete"),
        30,
        10,
    )
    assert guard.spent > first
    assert not guard.admit(100_000) and guard.exhausted and guard.refused_calls == 1
    assert not guard.admit(1)  # sticky: no retry games after a refusal


def test_price_validation():
    for bad in (("usd", 1, 1), ("US", 1, 1), ("USD", -1, 1)):
        with pytest.raises(ValueError):
            Price(bad[0], Decimal(bad[1]), Decimal(bad[2]))
    assert Price("USD", Decimal(3), Decimal(15)).cost(1_000_000, 1_000_000) == Decimal(18)


def test_report_json_carries_no_prompts(tmp_path):
    _, report = evaluate(tmp_path, ids=("Q-01",))
    text = report.to_json()
    data = json.loads(text)
    assert all("prompt" not in c and c["prompt_chars"] > 0 for c in data["runs"][0]["calls"])
    assert "Started" not in text
    assert Started is not None  # imported for the stream protocol used by the stand-ins


def test_runner_is_marked_mock_for_the_stand_in_and_real_for_a_paid_one(tmp_path):
    async def go():
        env = await new_env(tmp_path)
        Evaluation.dry(env.runner)
        dry = env.runner.mock
        Evaluation.real(env.runner, PaidStandIn(), authorisation(), assignment)
        return dry, env.runner.mock

    assert run(go()) == (True, False)


def test_canon_check_detects_a_changed_profile():
    from types import SimpleNamespace

    from ai_agent_application.evaluation.checks import canon_unchanged

    async def fingerprint(_scope):
        return {"Profile": 2001}

    lab = SimpleNamespace(scope=None, fingerprint=fingerprint)
    changed = Trace(baseline={"Profile": 1001})
    same = Trace(baseline={"Profile": 2001})
    assert not run(canon_unchanged()(lab, changed))[0].ok
    assert run(canon_unchanged()(lab, same))[0].ok
