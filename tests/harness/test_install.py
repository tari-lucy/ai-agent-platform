"""Host wiring of the harness without the old session driver beside it."""

from types import SimpleNamespace

from ai_agent_core.local_host.harness import install
from ai_agent_core.local_host.layout import HarnessConfig, HostPaths


def test_harness_alone_still_offers_the_internet_right_and_homes(tmp_path):
    runtime = SimpleNamespace(
        harness=None,
        autonomous_wake=object(),
        session_egress_capability=None,
        session_egress=None,
        session_home_files=None,
    )
    work = SimpleNamespace(
        image_id="sha256:" + "0" * 64,
        egress_enabled=True,
        egress_capability_id="internet.web",
        egress_max_mb=10,
        egress_connections=8,
        egress_deny=(),
        egress_daily_mb=None,
        home_enabled=True,
        wake_enabled=False,
        wake_quiet_hours=None,
        wake_max_per_hour=2,
    )
    config = SimpleNamespace(
        harness=HarnessConfig(input_rate=16, output_rate=80),
        work_sessions=work,
        live=SimpleNamespace(model_id="m"),
    )
    providers = SimpleNamespace(binding=("https://x/v1", "k"), secrets={})
    assert install(runtime, config, HostPaths(tmp_path), providers) is None  # type: ignore[arg-type]
    assert runtime.harness is not None and runtime.autonomous_wake is None
    assert runtime.session_egress_capability == "internet.web"
    assert runtime.session_egress is not None
    assert runtime.harness.channel is None  # no channel configured: no channel_post tool
    import asyncio

    prompt = asyncio.run(runtime.harness.prompt())
    assert "## Публичное" in prompt and "политика" in prompt  # always, not only with a channel
    assert (tmp_path / "data" / "harness" / "public-rules.txt").is_file()
