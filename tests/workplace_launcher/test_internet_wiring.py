"""W3 internet wired through the offline launcher and the operator's permission.

Synthetic model and no Docker: the gateway itself is in test_egress*/test_real_home.
"""

import asyncio

import pytest
from ai_agent_application.storage import Owner
from ai_agent_application.tickets import TicketDraft
from ai_agent_contracts.agent_workplace import CapabilityManifest, CapabilityRequest
from test_activity import WorkModel, prepared
from test_library_host import decide

from ai_agent_core.local_host import work_sessions
from ai_agent_core.local_host.layout import WorkSessionsConfig, load_config, paths
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.work_sessions import enable_work_sessions
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.activity import tools
from ai_agent_core.m3_composition.state import PRINCIPALS
from ai_agent_core.m3_composition.work_session_egress import GUIDE as EGRESS_GUIDE
from ai_agent_core.m3_composition.work_session_home import OFFLINE
from ai_agent_core.m3_composition.work_session_opencode import MODEL_ONLY, environment_guide

IMAGE = "sha256:" + "a" * 64
AREAS = ["projects", "inbox"]
INTERNET = "workplace.internet"


def test_configuration_bounds():
    base = dict(image_id=IMAGE, areas=("projects",), output_area="projects")
    off = WorkSessionsConfig.model_validate(base)
    assert not off.egress_enabled and off.egress_capability_id == INTERNET
    for changed in (
        dict(egress_deny=("not-a-network",)),
        dict(egress_capability_id="workplace.opencode-session"),
        dict(egress_capability_id="workplace.scripts"),
        dict(egress_max_mb=0),
    ):
        with pytest.raises(Exception, match="WORK_SESSIONS_LIMITS|greater"):
            WorkSessionsConfig.model_validate(dict(base, egress_enabled=True, **changed))


def test_guide_tells_the_truth_about_the_network():
    plain = environment_guide("projects", home=True)
    assert OFFLINE in plain and MODEL_ONLY in plain and EGRESS_GUIDE not in plain
    online = environment_guide("projects", home=True, internet=True)
    assert OFFLINE not in online and MODEL_ONLY not in online and EGRESS_GUIDE in online


def test_internet_is_seeded_but_usable_only_with_the_operators_permission(tmp_path, monkeypatch):
    async def scenario():
        host = await prepared(tmp_path, monkeypatch, WorkModel())
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/e.sqlite"
        )
        await enable_workplace_files(host.root)
        monkeypatch.setattr(work_sessions, "check", lambda settings, **unused: None)
        await enable_work_sessions(
            host.root, image_id=IMAGE, areas=AREAS, output_area="projects", ceiling_days=7,
            egress_enabled=True, egress_deny=["203.0.113.0/24"], egress_max_mb=64,
        )  # fmt: skip
        settings = load_config(paths(host.root)).work_sessions
        assert settings.egress_enabled and settings.egress_deny == ("203.0.113.0/24",)
        async with host.open():
            rt, scope = host.runtime, host.scope
            assert rt.session_egress_capability == INTERNET
            sessions = rt.session_harness.__self__
            assert sessions.egress.limits.max_bytes == 64 * 1024 * 1024
            assert sessions.egress.limits.deny == ("203.0.113.0/24",)
            assert sessions.egress.log_dir == paths(host.root).egress_log
            manifest = await rt.store.repository(
                CapabilityManifest, PRINCIPALS[Owner.WORKSPACE], scope
            ).get("manifest-" + INTERNET)
            assert manifest.effect == "external_effect" and manifest.network == "gateway"
            usable = {
                t.manifest.capability_id: t.available for t in await tools(rt, scope, session=True)
            }
            assert usable[INTERNET] is False  # seeded, not granted

            raised = await rt.tickets.raise_ticket(
                "need-internet", scope, "agent", "workplace-agent",
                TicketDraft(
                    kind="capability_request",
                    title="Нужен интернет",
                    summary="Скачать документацию и пакеты.",
                    attempted="Сети нет.",
                    decision_needed="Разрешить выход в интернет.",
                    basis=INTERNET,
                    request=CapabilityRequest(capability_id=INTERNET, constraints="Через шлюз."),
                ),
                "Прошу.",
            )  # fmt: skip
            await decide(
                host,
                "grant-internet",
                dict(
                    action="grant",
                    capability_id=INTERNET,
                    binding_id="opencode." + INTERNET,
                    ticket_id=raised.resource_id,
                    ticket_version=1,
                    level="automatic",
                    duration_seconds=86400,
                    areas=AREAS,
                    constraints="Через шлюз платформы.",
                ),
                1,
            )
            usable = {
                t.manifest.capability_id: t.available for t in await tools(rt, scope, session=True)
            }
            assert usable[INTERNET] is True
            assert rt.supervisor.snapshot.state.value == "STOPPED"

    asyncio.run(scenario())


def test_cli_dispatches_internet_arguments(monkeypatch, tmp_path):
    from ai_agent_core.local_host import cli

    calls = []

    async def sessions(root, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(cli, "enable_work_sessions", sessions)
    argv = ["enable-work-sessions", "--root", str(tmp_path), "--image-id", IMAGE]
    argv += ["--area", "projects", "--output-area", "projects"]
    assert cli.main(argv) == 0 and calls[-1]["egress_enabled"] is None
    extra = ["--internet", "--egress-max-mb", "64", "--egress-deny", "203.0.113.0/24"]
    assert cli.main([*argv, *extra, "--egress-deny", "198.51.100.0/24"]) == 0
    assert calls[-1]["egress_enabled"] is True and calls[-1]["egress_max_mb"] == 64
    assert calls[-1]["egress_deny"] == ["203.0.113.0/24", "198.51.100.0/24"]
