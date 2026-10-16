"""The shared restore rule is scope-aware: TEST assignments stay mock beside live WORK."""

from ai_agent_adapters.storage.policy import scope_key
from ai_agent_contracts.common import Scope

from ai_agent_core.local_host.layout import HostConfig, LiveConfig, assignments_fit


def test_restore_admits_live_work_and_mock_test_but_not_a_live_test_or_old_work_binding():
    config = HostConfig(
        workspace_id="synthetic",
        persona_id="alpha",
        work_space_id="work",
        test_space_ids=("test",),
        port=1024,
        permission_expires_at="2099-01-01T00:00:00Z",
        operator_controls=True,
        settings_space_id="settings",
        live=LiveConfig(
            model_id="m", attempt_limit=300, attempt_period="utc_day", gm={"enabled": True}
        ),
    )
    work = scope_key(
        Scope(
            workspace_id="synthetic", persona_id="alpha", memory_space_id="work", memory_mode="work"
        )
    )
    test = scope_key(
        Scope(
            workspace_id="synthetic", persona_id="alpha", memory_space_id="test", memory_mode="test"
        )
    )
    bindings = {
        (work, "LLMAssignment", "gm"): dict(
            role_id="game-master", connection_id="conn-live", model_id="m"
        ),
        (test, "LLMAssignment", "gm"): dict(
            role_id="game-master", connection_id="conn-mock", model_id="mock-game-master"
        ),
    }
    assert assignments_fit(config, bindings)
    bad = {
        **bindings,
        (test, "LLMAssignment", "gm"): dict(
            role_id="game-master", connection_id="conn-live", model_id="m"
        ),
    }
    assert not assignments_fit(config, bad)
    bad = {
        **bindings,
        (work, "LLMAssignment", "gm"): dict(
            role_id="game-master", connection_id="conn-mock", model_id="mock-game-master"
        ),
    }
    assert not assignments_fit(config, bad)
    bad = {
        **bindings,
        (work, "LLMAssignment", "gm"): dict(
            role_id="game-master", connection_id="conn-live", model_id="other"
        ),
    }
    assert not assignments_fit(config, bad)
