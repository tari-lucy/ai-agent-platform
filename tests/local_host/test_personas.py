"""Offline independent profiles and ordinary host HTTP; synthetic data, no paid calls."""

import asyncio
import sqlite3

import pytest
from ai_agent_application.laboratory.profile import REFERENCE_PROFILE
from ai_agent_application.storage import Owner
from ai_agent_contracts.common import Scope
from ai_agent_contracts.persona import Profile
from pydantic import ValidationError
from test_activity import WorkModel, fast, prepared

from ai_agent_core.local_backup.compat import IDENTITY, _expected_scopes
from ai_agent_core.local_backup.compose import compose
from ai_agent_core.local_backup.layout import layout
from ai_agent_core.local_host import personas
from ai_agent_core.local_host.cli import main
from ai_agent_core.local_host.init import initialise
from ai_agent_core.local_host.layout import (
    PERSONA_PENDING,
    HostConfig,
    HostError,
    InactivePersona,
    host_scopes,
    load_config,
    paths,
)
from ai_agent_core.local_host.personas import add_persona
from ai_agent_core.local_host.process import exclusive_host
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.serve import build
from ai_agent_core.m3_composition.state import PRINCIPALS


def stored(root):
    with sqlite3.connect(paths(root).database) as db:
        return db.execute(
            "SELECT scope_id,kind,id,version FROM records ORDER BY scope_id,kind,id"
        ).fetchall()


def test_legacy_layout_total_scope_budget_and_backup_identity():
    config = HostConfig(
        workspace_id="synthetic",
        persona_id="alpha",
        work_space_id="alpha-work",
        test_space_ids=("test",),
        port=8765,
        permission_expires_at="2099-01-01T00:00:00Z",
    )
    assert config.additional_personas == () and len(host_scopes(config)) == 2
    assert "additional_personas" not in config.model_dump()
    people = tuple(
        InactivePersona(
            persona_id=f"person-{n}", work_space_id=f"work-{n}", profile_digest="a" * 64
        )
        for n in range(6)
    )
    full = HostConfig.model_validate(config.model_dump() | {"additional_personas": people})
    assert len(host_scopes(full)) == 8 and "additional_personas" in IDENTITY
    assert len(_expected_scopes(full)) == 8
    for more in (
        people + (people[0],),
        people + (people[0].model_copy(update={"persona_id": "last"}),),
    ):
        with pytest.raises(ValidationError):
            HostConfig.model_validate(config.model_dump() | {"additional_personas": more})
    with pytest.raises(ValidationError):
        HostConfig.model_validate(
            config.model_dump()
            | {"additional_personas": (people[0].model_copy(update={"persona_id": "alpha"}),)}
        )


def test_cli_profile_import_replay_conflicts_and_lock(tmp_path, capsys):
    root = tmp_path / "host"
    asyncio.run(initialise(root, profile_markdown=REFERENCE_PROFILE, test_scopes=1))
    original = stored(root)
    profile = tmp_path / "profile.md"
    profile.write_text(REFERENCE_PROFILE, encoding="utf-8")
    args = [
        "add-persona",
        "--root",
        str(root),
        "--persona-id",
        "beta",
        "--profile-file",
        str(profile),
    ]
    assert main(args) == 0
    after = stored(root)
    assert set(original) <= set(after)
    assert len(after) == len(original) + 2  # Only Profile + CharacterState, no execution grants.
    config_bytes = paths(root).config.read_bytes()
    assert main(args) == 0 and stored(root) == after
    assert paths(root).config.read_bytes() == config_bytes
    profile.write_text(REFERENCE_PROFILE + "\n", encoding="utf-8")
    assert main(args) == 78 and "PERSONA_ALREADY_EXISTS" in capsys.readouterr().err
    with exclusive_host(root):
        with pytest.raises(HostError, match="HOST_ALREADY_RUNNING"):
            asyncio.run(add_persona(root, persona_id="gamma", profile_markdown=REFERENCE_PROFILE))
    with pytest.raises(HostError, match="PROFILE_INVALID"):
        asyncio.run(add_persona(root, persona_id="gamma", profile_markdown="not a profile"))
    with pytest.raises(HostError, match="INVALID_PERSONA_ID"):
        asyncio.run(add_persona(root, persona_id="../escape", profile_markdown=REFERENCE_PROFILE))
    assert stored(root) == after and not (root / PERSONA_PENDING).exists()


def test_capacity_refuses_before_profile_or_marker(tmp_path):
    async def scenario():
        root = tmp_path / "host"
        await initialise(root, profile_markdown=REFERENCE_PROFILE, test_scopes=7)
        before = stored(root)
        with pytest.raises(HostError, match="PERSONA_SCOPE_LIMIT"):
            await add_persona(root, persona_id="beta", profile_markdown=REFERENCE_PROFILE)
        assert before == stored(root) and not (root / PERSONA_PENDING).exists()

    asyncio.run(scenario())


def test_interrupted_import_closes_serve_and_replays_owner_commit(tmp_path, monkeypatch):
    async def scenario():
        root = tmp_path / "host"
        before = await initialise(root, profile_markdown=REFERENCE_PROFILE, test_scopes=1)
        save = personas.save_config

        def interrupted(*_):
            raise HostError("CONFIG_WRITE_FAILED")

        monkeypatch.setattr(personas, "save_config", interrupted)
        with pytest.raises(HostError, match="CONFIG_WRITE_FAILED"):
            await add_persona(root, persona_id="beta", profile_markdown=REFERENCE_PROFILE)
        pending_rows = stored(root)
        with pytest.raises(HostError, match="PERSONA_ADD_INCOMPLETE"):
            load_config(paths(root))
        with pytest.raises(HostError, match="PERSONA_ADD_INCOMPLETE"):
            build(paths(root), before, provider="mock")  # Cached config cannot bypass the marker.
        with pytest.raises(HostError, match="PERSONA_PENDING_DIFFERENT_REQUEST"):
            await add_persona(root, persona_id="gamma", profile_markdown=REFERENCE_PROFILE)
        monkeypatch.setattr(personas, "save_config", save)
        after = await add_persona(root, persona_id="beta", profile_markdown=REFERENCE_PROFILE)
        assert stored(root) == pending_rows and len(after.additional_personas) == 1
        assert load_config(paths(root)) == after
        assert not (root / PERSONA_PENDING).exists()

    asyncio.run(scenario())


def test_two_personas_on_real_host_http_are_isolated_and_only_a_can_run(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        config = await add_persona(host.root, persona_id="beta", profile_markdown=REFERENCE_PROFILE)
        other = host_scopes(config)[-1]

        async with host.open():
            rt, client = host.runtime, host.env.client
            fast(rt)
            assert rt.supervisor.snapshot.scope.partition == host.scope.partition
            assert len(rt.scopes) == len(host_scopes(config)) <= 8
            a_profile = await rt.store.repository(
                Profile, PRINCIPALS[Owner.PERSONA], host.scope
            ).get(host.scope.persona_id)
            b_profile = await rt.store.repository(Profile, PRINCIPALS[Owner.PERSONA], other).get(
                "beta"
            )
            assert a_profile.canon != b_profile.canon
            assert (
                await rt.store.repository(Profile, PRINCIPALS[Owner.PERSONA], other).get(
                    host.scope.persona_id
                )
                is None
            )

            async def send(scope: Scope, payload):
                return await client.post(
                    "/api/v1/commands",
                    json=payload | {"scope": scope.model_dump(mode="json")},
                    headers=host.env.headers,
                )

            def transcript(scope: Scope, sid: str) -> str:
                query = "&".join(
                    f"{key}={getattr(scope, key)}"
                    for key in ("workspace_id", "persona_id", "memory_space_id", "memory_mode")
                )
                return f"/api/v1/work-session-transcript/{sid}?{query}"

            opened = await send(
                other,
                dict(
                    type="work.session.open",
                    request_id="beta-open",
                    target="work",
                    arguments=dict(schema_id="work.session.open-args", data={"title": "Beta only"}),
                ),
            )
            assert opened.status_code == 200, opened.text
            sid = opened.json()["resource_id"]
            message = dict(
                type="work.session.message",
                request_id="beta-say",
                target=sid,
                expected_version=1,
                arguments=dict(
                    schema_id="work.session.message-args",
                    data={"session_id": sid, "text": "BETA_PRIVATE_MARKER"},
                ),
            )
            made = await send(other, message)
            assert made.status_code == 200, made.text
            assert (await send(other, message)).json() == made.json()
            endpoint = transcript(other, sid)
            view = await client.get(endpoint)
            assert (
                view.status_code == 200
                and view.json()["entries"][0]["text"] == "BETA_PRIVATE_MARKER"
            )
            foreign = await client.get(transcript(host.scope, sid))
            assert foreign.status_code != 200 and "BETA_PRIVATE_MARKER" not in foreign.text
            denied = await send(
                other,
                dict(
                    type="agent.start",
                    request_id="start-beta",
                    target="primary",
                    expected_version=rt.supervisor.snapshot.version,
                    arguments={"autonomy": "autonomous"},
                ),
            )
            assert denied.status_code == 403, denied.text
            await host.start()
            await asyncio.sleep(0.2)
            assert rt.supervisor.snapshot.scope.partition == host.scope.partition
            assert model.count() == 0
            assert (await client.get(endpoint)).json()["processed_sequence"] == 0
        async with host.open():
            assert host.runtime.supervisor.snapshot.state.value == "STOPPED"
            assert host.runtime.supervisor.snapshot.scope.partition == host.scope.partition
            assert model.count() == 0

    asyncio.run(scenario())


def test_backup_scope_identity_includes_new_persona(tmp_path):
    async def scenario():
        root = tmp_path / "host"
        await initialise(root, profile_markdown=REFERENCE_PROFILE, test_scopes=1)
        copies, ledger = tmp_path / "copies", tmp_path / "ledger/events.sqlite"
        await enable_recovery(root, store_dir=copies, ledger_path=ledger)
        place = layout(root, copies, ledger)
        first = compose(place, load_config(paths(root)))
        try:
            old = await first.service.create("manual")
        finally:
            first.close()
        await add_persona(root, persona_id="beta", profile_markdown=REFERENCE_PROFILE)
        current = compose(place, load_config(paths(root)))
        try:
            current.gate.held = True  # no live serve in this isolated fixture
            refused = await current.service.preview_restore(old.manifest.backup_id)
            codes = {issue.code for issue in refused.issues}
            assert {"HOST_CONFIG_INCOMPATIBLE", "SCOPES_INCOMPATIBLE"} <= codes
            new = await current.service.create("manual")
            accepted = await current.service.preview_restore(new.manifest.backup_id)
            assert not accepted.blocked, accepted.issues
        finally:
            current.close()

    asyncio.run(scenario())
