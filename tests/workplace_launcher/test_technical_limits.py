"""D76: per-turn and per-call caps are technical rails, high by default; money bounds spending."""

from ai_agent_core.local_host.layout import LiveConfig, WorkSessionsConfig
from ai_agent_core.local_host.work_sessions import template
from ai_agent_core.m3_composition.schemas import PermitLimits

IMAGE = "sha256:" + "a" * 64


def test_defaults_let_a_long_turn_run():
    settings = WorkSessionsConfig.model_validate(
        dict(image_id=IMAGE, areas=("projects",), output_area="projects")
    )
    assert settings.max_model_calls == 200 and settings.wall_seconds == 4 * 3600
    assert settings.max_model_tokens == 8192 and settings.max_prompt_chars == 400000
    built = template(settings)
    assert built.max_model_calls == 200 and built.max_prompt_chars == 400000
    live = LiveConfig(model_id="fixture-model")
    assert live.max_output_tokens == 8192 and live.max_prompt_chars == 400000
    limits = PermitLimits()
    assert limits.max_calls >= 1000 and limits.max_output_tokens >= live.max_output_tokens


def test_cpu_above_wall_is_clamped_not_refused():
    settings = WorkSessionsConfig.model_validate(
        dict(image_id=IMAGE, areas=("projects",), output_area="projects", wall_seconds=600)
    )
    assert template(settings).ceiling.cpu_seconds == 600
