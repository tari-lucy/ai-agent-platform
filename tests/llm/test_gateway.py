"""LLM gateway over a scripted provider (M2-04): streams, retry, fallback, cancel, results."""

import asyncio

import pytest
from ai_agent_adapters.llm import ScriptedProvider
from ai_agent_adapters.llm.mock import Pause
from ai_agent_application.llm import (
    Delta,
    Failure,
    Finished,
    ProviderError,
    Started,
    UsageReported,
    parse_result,
    resolve_assignment,
)
from llm_support import (
    CREDENTIAL_SETTING,
    ENDPOINT_SETTING,
    REGISTRY,
    SCOPE,
    Sleeps,
    assignment,
    connection,
    gateway,
    request,
    resolved,
    run,
    usage,
)
from pydantic import BaseModel, ConfigDict

FULL = [
    Started("provider-model-7"),
    Delta("При"),
    Delta("вет!"),
    Finished("stop"),
    UsageReported(usage(12, 4, cached=5, reasoning=1)),
]


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ok: bool
    note: str


def test_full_stream_completes_with_text_usage_and_reported_model():
    provider = ScriptedProvider([list(FULL)])
    reports, seen = [], []

    async def scenario():
        async def on_delta(text):
            seen.append(text)

        return await gateway(provider, reports=reports).generate(request(), on_delta=on_delta)

    result = run(scenario())
    assert result.status == "completed" and result.text == "Привет!" and result.failure is None
    assert seen == ["При", "вет!"]
    [report] = reports
    assert report == result.attempts[0]
    assert report.status == "completed" and report.requested_model_id == "model-x"
    assert report.reported_model_id == "provider-model-7"
    assert report.usage.input_tokens == 12 and report.usage.cached_input_tokens == 5
    assert provider.closed == 1
    assert [m.role for m in provider.calls[0].messages] == ["system", "user"]


def test_broken_stream_is_incomplete_without_result_and_usage_stays_unknown():
    provider = ScriptedProvider([[Started(None), Delta("Прив"), Delta("ет, ка")]])
    reports = []
    result = run(gateway(provider, reports=reports).generate(request()))
    assert result.status == "incomplete" and result.text is None and result.parsed is None
    assert result.preview == "Привет, ка"
    [report] = reports
    assert report.status == "incomplete" and report.usage.source == "unknown"
    assert report.usage.input_tokens is None and report.usage.output_tokens is None
    assert len(provider.calls) == 1  # text was shown: no silent re-ask


def test_stream_that_dies_with_an_error_after_text_is_not_retried():
    provider = ScriptedProvider(
        [[Delta("часть"), ProviderError(Failure.TRANSIENT)], [Delta("не должно"), Finished("stop")]]
    )
    result = run(gateway(provider).generate(request()))
    assert result.status == "incomplete" and result.preview == "часть" and len(provider.calls) == 1


def test_empty_stream_is_retried_once_then_fails_and_both_attempts_are_reported():
    provider = ScriptedProvider([[], []])
    reports, sleeps = [], Sleeps()
    result = run(gateway(provider, reports=reports, sleeps=sleeps).generate(request()))
    assert result.status == "failed" and result.failure is Failure.TRANSIENT
    assert [r.attempt_id for r in reports] == ["op-1.a1", "op-1.a2"]
    assert [r.status for r in reports] == ["unknown", "unknown"]
    assert len(sleeps.calls) == 1 and sleeps.calls[0] == pytest.approx(0.5)


def test_transient_failure_then_success_counts_both_real_attempts():
    provider = ScriptedProvider([[ProviderError(Failure.RATE_LIMITED, 2.0)], list(FULL)])
    reports, sleeps = [], Sleeps()
    result = run(gateway(provider, reports=reports, sleeps=sleeps).generate(request()))
    assert result.status == "completed" and len(result.attempts) == 2
    assert [r.status for r in reports] == ["failed", "completed"]
    assert sleeps.calls == [2.0]  # Retry-After honoured (jitter pinned to 1.0 in tests)


def test_retry_is_finite_and_the_pause_is_capped():
    provider = ScriptedProvider([[ProviderError(Failure.RATE_LIMITED, 900.0)]] * 5)
    sleeps = Sleeps()
    result = run(gateway(provider, sleeps=sleeps).generate(request()))
    assert result.status == "failed" and result.failure is Failure.RATE_LIMITED
    assert len(provider.calls) == 2 and sleeps.calls == [30.0]


def test_retry_jitter_is_inside_the_documented_range():
    from ai_agent_application.llm import LLMGateway

    for _ in range(50):
        gw = LLMGateway(lambda _a: None, lambda _r: resolved())
        assert 0.5 * 0.75 <= gw._delay(0, None) <= 0.5 * 1.25


def test_permanent_rejection_is_neither_retried_nor_sent_to_the_fallback():
    fallback = resolved("asg-b", model="model-y")
    primary = resolved("asg-a", fallback=fallback)
    provider = ScriptedProvider([[ProviderError(Failure.REJECTED)], list(FULL)])
    result = run(gateway(provider, primary).generate(request()))
    assert result.status == "failed" and result.failure is Failure.REJECTED
    assert len(provider.calls) == 1 and not result.used_fallback


def test_fallback_is_used_visibly_and_reported_under_its_own_assignment():
    fallback = resolved("asg-b", model="model-y")
    primary = resolved("asg-a", fallback=fallback)
    provider = ScriptedProvider([[ProviderError(Failure.AUTH)], list(FULL)])
    reports = []
    result = run(gateway(provider, primary, reports=reports).generate(request()))
    assert result.status == "completed" and result.used_fallback
    assert result.assignment_id == "asg-b"
    assert [(r.assignment_id, r.requested_model_id) for r in reports] == [
        ("asg-a", "model-x"),
        ("asg-b", "model-y"),
    ]
    assert provider.calls[1].assignment.model_id == "model-y"


def test_fallback_is_not_used_after_text_was_already_shown():
    fallback = resolved("asg-b", model="model-y")
    primary = resolved("asg-a", fallback=fallback)
    provider = ScriptedProvider([[Delta("нача"), ProviderError(Failure.AUTH)], list(FULL)])
    result = run(gateway(provider, primary).generate(request()))
    assert result.status == "incomplete" and len(provider.calls) == 1


def test_fallback_cycles_and_long_chains_are_refused_before_any_call():
    a = resolved("asg-a")
    b = resolved("asg-b", fallback=a)
    cyclic = type(a)(**{**a.__dict__, "fallback": b})
    provider = ScriptedProvider([list(FULL)])
    result = run(gateway(provider, cyclic).generate(request()))
    assert result.status == "failed" and result.failure is Failure.UNSUPPORTED
    assert provider.calls == []


def test_unknown_adapter_is_a_capability_failure_not_a_guess():
    provider = ScriptedProvider([list(FULL)])
    result = run(gateway(provider, resolved(adapter="nonexistent")).generate(request()))
    assert result.failure is Failure.UNSUPPORTED and provider.calls == []


def test_cancel_event_closes_a_stalled_stream_and_drops_late_events():
    provider = ScriptedProvider(
        [[Started(None), Delta("раз"), Pause(30), Delta("поздно"), Finished("stop")]]
    )
    reports, seen = [], []

    async def scenario():
        cancel = asyncio.Event()

        async def on_delta(text):
            seen.append(text)
            cancel.set()

        return await gateway(provider, reports=reports).generate(
            request(), cancel=cancel, on_delta=on_delta
        )

    result = run(scenario())
    assert result.status == "cancelled" and result.failure is Failure.CANCELLED
    assert result.text is None and seen == ["раз"] and "поздно" not in result.preview
    assert reports[0].status == "cancelled" and provider.closed == 1


def test_cancel_while_waiting_for_the_first_event():
    provider = ScriptedProvider([[Pause(30), Delta("x"), Finished("stop")]])

    async def scenario():
        cancel = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, cancel.set)
        return await gateway(provider).generate(request(), cancel=cancel)

    result = run(scenario())
    assert result.status == "cancelled" and provider.closed == 1


def test_task_cancellation_still_reports_the_attempt_and_closes_the_provider():
    provider = ScriptedProvider([[Started(None), Pause(30)]])
    reports = []

    async def scenario():
        task = asyncio.ensure_future(gateway(provider, reports=reports).generate(request()))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert [r.status for r in reports] == ["cancelled"] and provider.closed == 1
    assert reports[0].usage.source == "unknown"  # cancellation does not zero the cost


def test_revoked_admission_closes_output_and_drops_the_late_result():
    provider = ScriptedProvider([[Delta("раз"), Delta("два"), Finished("stop")]])
    admitted = {"yes": True}
    seen = []

    async def on_delta(text):
        seen.append(text)
        admitted["yes"] = False  # STOP arrives while the first fragment is shown

    result = run(
        gateway(provider, admission=lambda: admitted["yes"]).generate(request(), on_delta=on_delta)
    )
    assert result.status == "cancelled" and result.failure is Failure.NOT_ADMITTED
    assert result.text is None and seen == ["раз"]


def test_result_that_arrives_after_stop_has_no_effect():
    provider = ScriptedProvider([list(FULL)])
    state = {"calls": 0}

    def admission():
        state["calls"] += 1
        return state["calls"] < 8  # allowed during the stream, revoked before the hand-off

    result = run(gateway(provider, admission=admission).generate(request()))
    assert result.status == "cancelled" and result.text is None


def test_an_event_that_arrives_after_admission_was_revoked_is_dropped():
    provider = ScriptedProvider([[Delta("раз"), Pause(0.1), Delta("после STOP"), Finished("stop")]])
    admitted = {"yes": True}
    seen = []

    async def on_delta(text):
        seen.append(text)
        asyncio.get_running_loop().call_later(0.03, admitted.update, {"yes": False})

    result = run(
        gateway(provider, admission=lambda: admitted["yes"]).generate(request(), on_delta=on_delta)
    )
    assert result.status == "cancelled" and seen == ["раз"] and "STOP" not in result.preview


def test_admission_is_checked_once_more_after_the_stream_has_closed():
    state = {"closed": False}

    class Provider:
        def stream(self, call):
            return self._play()

        async def _play(self):
            try:
                yield Delta("итог")
                yield Finished("stop")
            finally:
                state["closed"] = True

    result = run(gateway(Provider(), admission=lambda: not state["closed"]).generate(request()))
    assert result.status == "cancelled" and result.failure is Failure.NOT_ADMITTED
    assert result.text is None and result.attempts[0].status == "completed"


def test_attempts_per_assignment_are_bounded_by_construction():
    from ai_agent_application.llm import LLMGateway

    for bad in (0, 3, 10):
        with pytest.raises(ValueError):
            LLMGateway(lambda _a: None, lambda _r: resolved(), max_attempts=bad)


def test_not_admitted_before_the_first_call_never_reaches_the_provider():
    provider = ScriptedProvider([list(FULL)])
    result = run(gateway(provider, admission=lambda: False).generate(request()))
    assert result.status == "cancelled" and provider.calls == []


def test_timeout_is_an_unknown_outcome_and_bounded():
    provider = ScriptedProvider([[Started(None), Pause(30)]] * 2)
    fast = resolved(parameters={"max_output_tokens": 16, "timeout_seconds": 0.05})
    reports = []
    result = run(gateway(provider, fast, reports=reports, max_attempts=1).generate(request()))
    assert result.failure is Failure.TIMEOUT and result.status == "failed"
    assert [r.status for r in reports] == ["unknown"] and provider.closed == 1


@pytest.mark.parametrize(
    ("reason", "failure"),
    [
        ("length", Failure.TRUNCATED),
        ("content_filter", Failure.FILTERED),
        ("tool_calls", Failure.UNSUPPORTED),
        ("other", Failure.PROTOCOL),
    ],
)
def test_non_stop_finish_reasons_never_complete(reason, failure):
    provider = ScriptedProvider([[Delta('{"ok": tr'), Finished(reason)]] * 3)
    result = run(gateway(provider, max_attempts=1).generate(request(result_model=Verdict)))
    assert result.status != "completed" and result.text is None and result.parsed is None
    assert result.failure is failure
    assert result.status == "incomplete" and result.preview == '{"ok": tr'  # shown, not a result


def test_text_after_the_terminal_event_is_a_protocol_error():
    provider = ScriptedProvider([[Delta("а"), Finished("stop"), Delta("б")]] * 2)
    result = run(gateway(provider, max_attempts=1).generate(request()))
    assert result.status != "completed"


def test_empty_completion_is_not_a_result():
    provider = ScriptedProvider([[Finished("stop")]])
    result = run(gateway(provider).generate(request()))
    assert result.failure is Failure.MALFORMED_RESULT and result.text is None


def test_late_usage_replaces_the_estimate_and_is_not_added_twice():
    provider = ScriptedProvider(
        [[Delta("a"), UsageReported(usage(3, 1)), Finished("stop"), UsageReported(usage(12, 4))]]
    )
    reports = []
    run(gateway(provider, reports=reports).generate(request()))
    assert (reports[0].usage.input_tokens, reports[0].usage.output_tokens) == (12, 4)


def test_missing_usage_stays_unknown_not_zero_on_a_completed_call():
    provider = ScriptedProvider([[Delta("a"), Finished("stop")]])
    reports = []
    result = run(gateway(provider, reports=reports).generate(request()))
    assert result.status == "completed"
    assert reports[0].usage.source == "unknown" and reports[0].usage.input_tokens is None


@pytest.mark.parametrize(
    "text",
    [
        '{"ok": true}',  # missing field
        '{"ok": true, "note": "x", "extra": 1}',  # unknown field
        '```json\n{"ok": true, "note": "x"}\n```',  # fenced
        '{"ok": true, "ok": false, "note": "x"}',  # duplicate key
        '{"ok": "yes", "note": "x"}',  # wrong type
        '{"ok": true, "note": NaN}',  # non-finite constant
        '[{"ok": true, "note": "x"}]',  # not an object
        '{"ok": true, "note": "x"} trailing',
        '{"ok": tru',  # cut off
    ],
)
def test_malformed_results_are_failures_and_never_parsed(text):
    assert parse_result(text, Verdict) is None
    provider = ScriptedProvider([[Delta(text), Finished("stop"), UsageReported(usage())]])
    reports = []
    result = run(gateway(provider, reports=reports).generate(request(result_model=Verdict)))
    assert result.status == "failed" and result.failure is Failure.MALFORMED_RESULT
    assert result.parsed is None and result.text is None
    assert reports[0].status == "completed"  # the call happened and is billed; the content failed
    assert len(provider.calls) == 1  # a malformed answer is not silently re-asked


def test_a_valid_document_is_parsed_only_after_completion():
    provider = ScriptedProvider(
        [[Delta('{"ok": true, '), Delta('"note": "ага"}'), Finished("stop")]]
    )
    result = run(gateway(provider).generate(request(result_model=Verdict)))
    assert result.status == "completed"
    assert result.parsed == Verdict(ok=True, note="ага")


def test_the_provider_receives_no_tools_and_adapter_errors_do_not_leak_text():
    class Boom:
        def stream(self, call):
            raise RuntimeError("secret-url https://x.invalid/?key=SUPERSECRET")

    result = run(gateway(Boom(), max_attempts=1).generate(request()))
    assert result.failure is Failure.PROTOCOL
    assert "SUPERSECRET" not in repr(result)


# -- role binding -------------------------------------------------------------------------


def test_model_and_endpoint_are_configuration_not_code():
    one = resolved("asg-a", model="model-one")
    two = resolve_assignment(
        assignment("asg-a", model="model-two"),
        connection("conn-a", endpoint="AI_PLATFORM_OTHER_ENDPOINT"),
        REGISTRY,
    )
    assert one.model_id == "model-one" and two.model_id == "model-two"
    assert (
        one.endpoint_setting == ENDPOINT_SETTING
        and two.endpoint_setting == "AI_PLATFORM_OTHER_ENDPOINT"
    )
    assert one.credential_setting == CREDENTIAL_SETTING


@pytest.mark.parametrize(
    ("conn", "asg"),
    [
        (connection(configured=False), assignment()),
        (connection(verification="failed"), assignment()),
        (connection(capabilities=("other",)), assignment()),
        (connection(), assignment(verification="failed")),
        (connection(), assignment(connection_id="conn-zzz")),
        (
            connection(),
            assignment(schema_id="llm.other-parameters", parameters={"max_output_tokens": 5}),
        ),
        (
            connection(),
            assignment(scope=SCOPE.model_copy(update={"memory_mode": "test"})),
        ),
    ],
)
def test_unusable_bindings_are_refused(conn, asg):
    with pytest.raises(ProviderError) as info:
        resolve_assignment(asg, conn, REGISTRY)
    assert info.value.failure is Failure.UNSUPPORTED


def test_a_fallback_must_be_the_declared_one_of_the_same_role():
    other_role = resolved("asg-b", role="game-master")
    with pytest.raises(ProviderError):
        resolve_assignment(assignment(fallback="asg-b"), connection(), REGISTRY, other_role)
    undeclared = resolved("asg-c")
    with pytest.raises(ProviderError):
        resolve_assignment(assignment(), connection(), REGISTRY, undeclared)
