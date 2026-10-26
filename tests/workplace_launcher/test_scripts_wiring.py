"""Managed Python helpers wired to ordinary local work conversations.

The offline `enable-work-sessions` configuration turns the helper container on: this suite
checks the configuration bounds, the seeded scripts capability, and the real runtime wiring
through ordinary serve. The permission surface is the operator's one-time decision that
answers the person's capability_request ticket; without it every helper call is refused.
Synthetic model and no Docker: the container mechanics have their own suites
(test_helpers, test_helper_transport); what is proven here is the connection.
"""

import asyncio

import pytest
from ai_agent_application.storage import Owner
from ai_agent_application.tickets import TicketDraft
from ai_agent_contracts.agent_workplace import (
    CapabilityBinding,
    CapabilityManifest,
    CapabilityRequest,
)
from test_activity import WorkModel, prepared
from test_library_host import decide

from ai_agent_core.local_host import work_sessions
from ai_agent_core.local_host.layout import (
    WorkSessionsConfig,
    load_config,
    paths,
)
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.work_sessions import (
    enable_work_sessions,
    helper_template,
    template,
)
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.activity import tools
from ai_agent_core.m3_composition.state import PRINCIPALS
from ai_agent_core.m3_composition.workplace import granted_areas

CAPABILITY = "workplace.opencode-session"
HELPER = "workplace.scripts"
AREAS = ["projects", "inbox"]
IMAGE = "sha256:" + "a" * 64


def test_helper_configuration_bounds_and_off_by_default():
    base = dict(image_id=IMAGE, areas=("projects", "inbox"), output_area="projects")
    off = WorkSessionsConfig.model_validate(base)
    assert helper_template(off) is None and template(off).max_helper_calls == 0
    on = WorkSessionsConfig.model_validate(dict(base, helper_enabled=True, wall_seconds=600))
    helper = helper_template(on)
    assert helper is not None
    assert helper.id == on.capability_id and on.helper_capability_id == HELPER
    assert template(on).max_helper_calls == on.helper_max_calls
    from ai_agent_core.m3_composition.work_session_helpers import PREFIX as HELPER_PREFIX

    assert helper.executable_prefix == HELPER_PREFIX
    assert helper.model_role_id is None and helper.max_workspace_calls == 0
    assert helper.ceiling.wall_seconds < on.wall_seconds
    assert helper.ceiling.memory_mb == on.helper_memory_mb
    assert helper.max_input_bytes == on.helper_max_input_bytes
    for changed in (
        dict(helper_enabled=True, helper_wall_seconds=600),
        dict(helper_enabled=True, helper_capability_id=CAPABILITY),
        dict(helper_enabled=True, helper_cpu_seconds=on.helper_wall_seconds + 1),
    ):
        with pytest.raises(Exception, match="WORK_SESSIONS_LIMITS"):
            WorkSessionsConfig.model_validate(dict(base, wall_seconds=600, **changed))


async def configured(tmp_path, monkeypatch, model, *, helper: bool):
    host = await prepared(tmp_path, monkeypatch, model)
    await enable_recovery(
        host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
    )
    await enable_workplace_files(host.root)
    # No Docker on the test machine: only the readiness probe is replaced, as in the launcher suite.
    monkeypatch.setattr(work_sessions, "check", lambda settings, **unused: None)
    await enable_work_sessions(
        host.root,
        image_id=IMAGE,
        areas=AREAS,
        output_area="projects",
        ceiling_days=7,
        helper_enabled=helper,
        wall_seconds=600,
        helper_wall_seconds=300,
        helper_max_input_bytes=134217728,
    )
    return host


def test_enable_seeds_scripts_capability_and_wires_the_runtime(tmp_path, monkeypatch):
    async def scenario():
        host = await configured(tmp_path, monkeypatch, WorkModel(), helper=True)
        settings = load_config(paths(host.root)).work_sessions
        assert settings.helper_enabled and settings.helper_capability_id == HELPER
        assert settings.helper_max_input_bytes == 134217728
        async with host.open():
            rt, scope = host.runtime, host.scope
            assert rt.session_capability == CAPABILITY
            assert rt.session_helper_capability == HELPER
            workspace = PRINCIPALS[Owner.WORKSPACE]
            helper_manifest = await rt.store.repository(
                CapabilityManifest, workspace, scope
            ).get("manifest-" + HELPER)
            assert helper_manifest is not None
            assert helper_manifest.network == "none" and helper_manifest.runner_required
            helper_binding = await rt.store.repository(CapabilityBinding, workspace, scope).get(
                "opencode." + HELPER
            )
            assert helper_binding is not None and helper_binding.state == "ready"
            session_manifest = await rt.store.repository(
                CapabilityManifest, workspace, scope
            ).get("manifest-" + CAPABILITY)
            assert HELPER in session_manifest.instructions  # the person can discover scripts
            listed = {tool.manifest.capability_id for tool in await tools(rt, scope, session=True)}
            assert {CAPABILITY, HELPER} <= listed

            # Without the operator's decision every helper call is refused.
            assert not await granted_areas(rt, scope, HELPER)

            # The person asks; the operator answers with one structured decision.
            raised = await rt.tickets.raise_ticket(
                "need-scripts",
                scope,
                "agent",
                "workplace-agent",
                TicketDraft(
                    kind="capability_request",
                    title="Нужен запуск Python-скриптов",
                    summary="Разобрать переданный архив своим скриптом.",
                    attempted="Без исполнения не получается.",
                    decision_needed="Разрешить запуск скриптов.",
                    basis=HELPER,
                    request=CapabilityRequest(
                        capability_id=HELPER, constraints="Разбор переданных материалов."
                    ),
                ),
                "Прошу.",
            )
            await decide(
                host,
                "grant-scripts",
                dict(
                    action="grant",
                    capability_id=HELPER,
                    binding_id="opencode." + HELPER,
                    ticket_id=raised.resource_id,
                    ticket_version=1,
                    level="automatic",
                    duration_seconds=86400,
                    areas=AREAS,
                    constraints="Разбор переданных материалов в пределах областей.",
                ),
                1,
            )
            assert {"inbox", "projects"} <= set(await granted_areas(rt, scope, HELPER))
            assert rt.supervisor.snapshot.state.value == "STOPPED"
        # Re-running the same configuration is idempotent; the manifest is never rewritten.
        await enable_work_sessions(
            host.root,
            image_id=IMAGE,
            areas=AREAS,
            output_area="projects",
            ceiling_days=7,
            helper_enabled=True,
            wall_seconds=600,
            helper_wall_seconds=300,
        )
        assert load_config(paths(host.root)).work_sessions.helper_enabled

    asyncio.run(scenario())


def test_without_helper_the_runtime_stays_off(tmp_path, monkeypatch):
    async def scenario():
        host = await configured(tmp_path, monkeypatch, WorkModel(), helper=False)
        async with host.open():
            rt, scope = host.runtime, host.scope
            assert rt.session_capability == CAPABILITY
            assert rt.session_helper_capability is None
            assert not await granted_areas(rt, scope, HELPER)
            listed = {tool.manifest.capability_id for tool in await tools(rt, scope, session=True)}
            assert HELPER not in listed and CAPABILITY in listed

    asyncio.run(scenario())
