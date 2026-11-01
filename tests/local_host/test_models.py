"""Choosing the persona's model: list, test one tiny request, switch offline."""

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_agent_core.local_host import models
from ai_agent_core.local_host.layout import HostError, LiveConfig

ROOT, ENV = Path("/r"), Path("/e")


def fake_binding(monkeypatch, replies):
    monkeypatch.setattr(models, "_binding", lambda root, env: ("https://p/v1", "KEY", "now-model"))
    calls = []

    def call(url, key, body, timeout):
        calls.append((url, key, body))
        return replies.pop(0)

    monkeypatch.setattr(models, "_call", call)
    return calls


def test_list_marks_the_current_model_and_a_failure_is_a_fixed_code(monkeypatch):
    calls = fake_binding(
        monkeypatch, [(200, {"data": [{"id": "b"}, {"id": "a"}, {"id": "a"}, {"x": 1}]})]
    )
    assert models.list_models(ROOT, ENV) == ("now-model", ["a", "b"])
    assert calls[0][0].endswith("/models") and calls[0][1] == "KEY"
    fake_binding(monkeypatch, [(503, None)])
    with pytest.raises(HostError) as error:
        models.list_models(ROOT, ENV)
    assert "503" in error.value.code and "KEY" not in error.value.code


def test_a_test_request_says_whether_the_model_answers(monkeypatch):
    calls = fake_binding(monkeypatch, [(200, {"choices": [{"message": {"content": "да"}}]})])
    ok, text = models.test_model(ROOT, ENV, "m1")
    assert ok and "отвечает" in text
    assert calls[0][2]["model"] == "m1" and calls[0][2]["max_tokens"] <= 16  # tiny and cheap

    fake_binding(monkeypatch, [(404, {"error": {"message": "model not available"}})])
    ok, text = models.test_model(ROOT, ENV, "gone")
    assert not ok and "404" in text and "model not available" in text

    fake_binding(monkeypatch, [(0, None)])
    ok, text = models.test_model(ROOT, ENV, "x")
    assert not ok and "нет связи" in text


def test_switching_stores_the_new_id_and_refuses_a_bad_one(monkeypatch):
    live = LiveConfig(model_id="old-model")
    saved = []

    class Config(SimpleNamespace):
        def model_copy(self, update):
            return SimpleNamespace(**{**vars(self), **update})

    monkeypatch.setattr(models, "paths", lambda root: SimpleNamespace(root=root))
    monkeypatch.setattr(models, "exclusive_host", lambda root: nullcontext())
    monkeypatch.setattr(models, "load_config", lambda host: Config(live=live))
    monkeypatch.setattr(models, "save_config", lambda host, config: saved.append(config))

    assert models.set_model(ROOT, "claude-opus-4-7") == "old-model"
    assert saved[0].live.model_id == "claude-opus-4-7"
    assert saved[0].live.attempt_limit == live.attempt_limit  # the rest is kept as it was
    with pytest.raises(HostError):
        models.set_model(ROOT, "bad model;rm")
    assert len(saved) == 1
