"""Ordered models with their own prices: the cost follows the model that answered."""

import json

import pytest
from pydantic import ValidationError

from ai_agent_core.local_host.cli import _parse_models
from ai_agent_core.local_host.layout import HarnessConfig, HostError
from ai_agent_core.m3_composition.harness_host import HarnessSettings, Usage

MODELS = (
    ("cx/gpt-6.1-sol", 0.10, 0.10, None),
    ("cx/gpt-6.1-terra", 0.075, 0.075, 0.05),
)


def usage(tmp_path, models=MODELS):
    return Usage(
        HarnessSettings(
            image_id="sha256:" + "0" * 64,
            port=4096,
            password="p",
            upstream="https://x/v1",
            key="k",
            model="cx/gpt-6.1-sol",
            usage_dir=tmp_path,
            daily_budget=4,
            input_rate=0.10,
            output_rate=0.10,
            currency="USD",
            models=models,
        )
    )


def test_the_cost_uses_the_price_of_the_model_that_answered(tmp_path):
    ledger = usage(tmp_path)
    row = {"input": 1_000_000, "output": 1_000_000, "cached": None}
    assert ledger.cost({**row, "model": "cx/gpt-6.1-sol"}) == pytest.approx(0.20)
    assert ledger.cost({**row, "model": "cx/gpt-6.1-terra"}) == pytest.approx(0.15)
    # an unknown or missing model falls back to the default rates, never to zero
    assert ledger.cost({**row, "model": "other"}) == pytest.approx(0.20)
    assert ledger.cost(row) == pytest.approx(0.20)
    cached = {"input": 1_000_000, "output": 0, "cached": 1_000_000, "model": "cx/gpt-6.1-terra"}
    assert ledger.cost(cached) == pytest.approx(0.05)  # that model's own cached price


def test_the_ledger_keeps_the_model_name_and_a_fallback_row_is_only_a_warning(tmp_path):
    ledger = usage(tmp_path)
    ledger.record({"kind": "fallback", "model": "cx/gpt-6.1-sol", "status": 503})
    assert not list(tmp_path.glob("harness-*.jsonl"))  # nothing billed for a model that gave way
    ledger.record(
        {"kind": "usage", "status": 200, "model": "cx/gpt-6.1-terra", "input": 100, "output": 5}
    )
    [entry] = [
        json.loads(x) for f in tmp_path.glob("harness-*.jsonl") for x in f.read_text().splitlines()
    ]
    assert entry["model"] == "cx/gpt-6.1-terra" and entry["cost"] == pytest.approx(
        0.0000079, abs=1e-6
    )


def test_the_models_option_is_parsed_strictly():
    rows = _parse_models("cx/a=0.10/0.10, cx/b=0.075/0.075/0.05")
    assert rows == [
        {"name": "cx/a", "input_rate": 0.10, "output_rate": 0.10},
        {"name": "cx/b", "input_rate": 0.075, "output_rate": 0.075, "cached_rate": 0.05},
    ]
    assert _parse_models(None) is None and _parse_models("none") == []
    for bad in ("cx/a", "cx/a=1", "cx/a=x/y", "=1/1", "cx/a=1/2/3/4"):
        with pytest.raises(HostError):
            _parse_models(bad)
    config = HarnessConfig(models=_parse_models("cx/a=0.1/0.1"))  # type: ignore[arg-type]
    assert config.models[0].name == "cx/a"
    with pytest.raises(ValidationError):
        HarnessConfig(models=[{"name": "bad name;x", "input_rate": 1, "output_rate": 1}])  # type: ignore[list-item]
