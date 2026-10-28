"""The harness budget charges cached input at the cached rate; unknown is never zero."""

from pathlib import Path

from ai_agent_core.m3_composition.harness_host import HarnessSettings, Usage


def settings(cached_rate: float | None) -> HarnessSettings:
    return HarnessSettings(
        image_id="sha256:" + "0" * 64,
        port=4096,
        password="p",
        upstream="https://example.invalid/v1",
        key="k",
        model="m",
        usage_dir=Path("/nonexistent"),
        daily_budget=3000.0,
        input_rate=16.0,
        output_rate=80.0,
        cached_rate=cached_rate,
    )


def test_cached_input_costs_the_cached_rate():
    usage = Usage(settings(1.6))
    # 10k input of which 8k cached, 500 output: 2k*16 + 8k*1.6 + 500*80 per 1M.
    assert abs(usage.cost({"input": 10000, "cached": 8000, "output": 500}) - 0.0848) < 1e-9


def test_without_a_cached_rate_or_report_everything_is_full_input():
    assert Usage(settings(None)).cost({"input": 10000, "cached": 8000, "output": 0}) == 0.16
    assert Usage(settings(1.6)).cost({"input": 10000, "output": 0}) == 0.16
    # Unknown input: the conservative estimate, and a reported cache cannot discount it.
    assert Usage(settings(1.6)).cost({"cached": 32000, "output": 0}) == 32000 * 16 / 1_000_000
