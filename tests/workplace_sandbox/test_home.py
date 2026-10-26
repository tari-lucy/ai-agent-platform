"""W2 home: volume naming, guide bounds, mounts and OpenCode permissions; no Docker."""

import pytest
from ai_agent_adapters.agent_harness.worker import HOME_INSTRUCTIONS, configuration
from ai_agent_adapters.sandbox.policy import GUIDE_BYTES, Home, RunnerTemplate, create_args
from ai_agent_application.agent_workplace.ports import RunnerLimits, RunnerTask
from ai_agent_contracts.common import Scope

from ai_agent_core.m3_composition.work_session_home import (
    DEFAULT_RULES,
    HomeSettings,
    home,
    rules,
    volume,
)

CAP = RunnerLimits(3, 128, 16, 3, 8192, 1024 * 1024)
TEMPLATE = RunnerTemplate("python-task", "sha256:" + "a" * 64, ("/usr/local/bin/python",), CAP, 1)
TASK = RunnerTask("python-task", ("/usr/local/bin/python", "-c", "pass"), (), CAP)
NAME = "aw-task-" + "b" * 32


def scope(mode="work", persona="sample"):
    return Scope(
        workspace_id="synthetic", persona_id=persona, memory_space_id="space", memory_mode=mode
    )


def test_one_volume_per_persona_partition():
    assert volume(scope()) == volume(scope())
    assert volume(scope()) != volume(scope(mode="test"))
    assert volume(scope()) != volume(scope(persona="other"))
    assert volume(scope()).startswith("aw-home-") and len(volume(scope())) == 40


def test_home_mount_only_when_asked_and_only_a_named_volume():
    plain = create_args(TEMPLATE, TASK, NAME, "pass")
    assert "--mount" not in plain and not any("/guide" in arg for arg in plain)
    vol = volume(scope())
    args = create_args(TEMPLATE, TASK, NAME, "pass", vol)
    assert f"type=volume,src={vol},dst=/workspace" in args
    assert any(arg.startswith("/guide:") and "noexec" in arg for arg in args)
    assert "--read-only" in args and "--network=none" in args
    for bad in ("/home/user", "aw-home-x", "aw-home-" + "B" * 32, "../aw-home-" + "a" * 32):
        with pytest.raises(ValueError):
            create_args(TEMPLATE, TASK, NAME, "pass", bad)


def test_home_texts_are_bounded():
    vol = volume(scope())
    with pytest.raises(ValueError):
        Home(vol, "x" * (GUIDE_BYTES + 1), "", "")
    with pytest.raises(ValueError):
        Home(vol, "", "", "", quota_mb=1)
    assert Home(vol, "r", "c", "m", 64).wire() == {
        "rules": "r",
        "connections": "c",
        "readme": "m",
        "quota_mb": 64,
    }


def test_operator_rules_are_read_fresh_and_bounded(tmp_path):
    path = tmp_path / "AGENTS.md"
    assert rules(None) == rules(path) == DEFAULT_RULES
    path.write_text("Синтетическое правило", encoding="utf-8")
    assert rules(path) == "Синтетическое правило"
    path.write_text("я" * GUIDE_BYTES, encoding="utf-8")
    clipped = rules(path)
    assert len(clipped.encode()) <= GUIDE_BYTES and clipped.endswith("размера]\n")
    made = home(HomeSettings(128, path), scope(), "Synthetic facts")
    assert made.volume == volume(scope()) and made.quota_mb == 128
    assert "Synthetic facts" in made.connections


def test_opencode_gets_a_shell_and_guide_only_with_a_home():
    plain = configuration("http://127.0.0.1:1/v1", 64)
    agent = plain["agent"]["platform"]["permission"]
    assert "bash" not in agent and plain["instructions"] == []
    with_home = configuration("http://127.0.0.1:1/v1", 64, home=True)
    agent = with_home["agent"]["platform"]["permission"]
    assert agent["*"] == "deny" and agent["bash"] == "allow"
    assert agent["external_directory"] == "allow"
    assert agent["edit"]["workspace/*"] == "allow" and agent["edit"]["*"] == "deny"
    assert agent["read"] == {"*": "allow"}
    assert "webfetch" not in agent and "task" not in agent
    assert with_home["instructions"] == HOME_INSTRUCTIONS
