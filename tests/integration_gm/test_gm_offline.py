"""Explicit offline GM switch: host lock, marker, one live-role rule, restore compatibility."""

import asyncio
import json
from datetime import UTC, datetime

import pytest
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_adapters.storage.policy import scope_key
from ai_agent_application.storage import Owner
from ai_agent_contracts.llm import LLMAssignment
from quality_support import PUBLIC_PROFILE

from ai_agent_core.local_host.gm import configure_gm
from ai_agent_core.local_host.init import initialise, work_scope
from ai_agent_core.local_host.layout import (
    GM_PENDING,
    HostError,
    LiveConfig,
    assignments_fit,
    load_config,
    paths,
)
from ai_agent_core.local_host.process import exclusive_host
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS, extension, put


async def bindings(root):
    config = load_config(paths(root), allow_gm_pending=True)
    scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
    store = SQLiteStore(paths(root).database, extensions=new_registry())
    try:
        rows = await store.repository(LLMAssignment, PRINCIPALS[Owner.LLM], scope).list(limit=100)
    finally:
        await store.close()
    return {
        (scope_key(scope), "LLMAssignment", r.id): dict(
            role_id=r.role_id, connection_id=r.connection_id, model_id=r.model_id
        )
        for r in rows
    }


def test_auto_start_toggle_does_not_rebind_tuned_model_limits(tmp_path):
    async def scenario():
        root = tmp_path / "host"
        await initialise(
            root,
            profile_markdown=PUBLIC_PROFILE.read_text(encoding="utf-8"),
            operator_controls=True,
            live=LiveConfig(model_id="fixture-model", attempt_limit=40, attempt_period="utc_day"),
        )
        await configure_gm(root, enabled=True, line_direction="Synthetic line.")
        cfg = load_config(paths(root))
        scope = work_scope(cfg.workspace_id, cfg.persona_id, cfg.work_space_id)
        registry = new_registry()
        store = SQLiteStore(paths(root).database, extensions=registry)
        try:
            old = await store.repository(LLMAssignment, PRINCIPALS[Owner.LLM], scope).get(
                "asg-game-master"
            )
            tuned = old.model_copy(
                update={
                    "parameters": extension(
                        registry,
                        "llm.generation-parameters",
                        {
                            "max_output_tokens": 512,
                            "timeout_seconds": 45.0,
                        },
                    ),
                    "version": old.version + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            await put(store, registry, Owner.LLM, scope, "tune-gm-limit", tuned, old.version)
        finally:
            await store.close()
        before = paths(root).database.read_bytes()
        await configure_gm(root, auto_start=True)
        assert load_config(paths(root)).live.gm.auto_start
        assert paths(root).database.read_bytes() == before
        await configure_gm(root, auto_start=False)
        assert not load_config(paths(root)).live.gm.auto_start
        assert paths(root).database.read_bytes() == before

    asyncio.run(scenario())


def live(rows):
    return sorted(v["role_id"] for v in rows.values() if v["connection_id"] == "conn-live")


def test_enable_disable_are_explicit_locked_resumable_and_restore_aware(tmp_path):
    async def scenario():
        root = tmp_path / "host"
        await initialise(
            root,
            profile_markdown=PUBLIC_PROFILE.read_text(encoding="utf-8"),
            operator_controls=True,
            live=LiveConfig(model_id="fixture-model", attempt_limit=40, attempt_period="utc_day"),
        )
        out = {
            "default": load_config(paths(root)).live.gm.enabled,
            "before": live(await bindings(root)),
        }
        for changes, code in (
            ({"enabled": True}, "GM_LINE_REQUIRED"),
            ({"enabled": True, "line_direction": "x", "daily_calls": 40}, "INVALID_OPTION"),
        ):
            with pytest.raises(HostError, match=code):
                await configure_gm(root, **changes)
        with exclusive_host(root), pytest.raises(HostError, match="HOST_ALREADY_RUNNING"):
            await configure_gm(root, enabled=True, line_direction="Линия.")
        (root / GM_PENDING).write_text("interrupted")
        with pytest.raises(HostError, match="GM_CHANGE_INCOMPLETE"):
            load_config(paths(root))  # serve stays closed until the change is finished
        before = await bindings(root)
        settings = await configure_gm(root, enabled=True, line_direction="Линия.", daily_offers=1)
        after = await bindings(root)
        config = load_config(paths(root))
        out["enabled"] = (
            settings.enabled,
            settings.daily_offers,
            settings.daily_calls,
            settings.interval_minutes,
        )
        out["after"] = live(after)
        out["marker"] = (root / GM_PENDING).exists()
        out["fit"] = (assignments_fit(config, after), assignments_fit(config, before))
        again = paths(root).database.read_bytes()
        await configure_gm(root, enabled=True)  # idempotent: nothing to rebind
        out["idempotent"] = paths(root).database.read_bytes() == again
        await configure_gm(root, enabled=False)
        off = await bindings(root)
        out["off"] = (live(off), assignments_fit(load_config(paths(root)), after))
        raw = json.loads(paths(root).config.read_text())
        raw["live"].pop("gm")  # a host prepared before this change
        paths(root).config.write_text(json.dumps(raw))
        out["old_host"] = load_config(paths(root)).live.gm.enabled
        return out

    out = asyncio.run(scenario())
    assert out["default"] is False and out["before"] == ["character"]
    assert out["enabled"] == (True, 1, 4, 60)
    assert out["after"] == ["character", "game-master", "story-screen"] and not out["marker"]
    assert out["fit"] == (True, False)  # a snapshot from before the switch is not compatible
    assert out["idempotent"]
    assert out["off"] == (["character"], False)
    assert out["old_host"] is False


def test_the_gm_budget_must_fit_beside_memory_and_dialogue():
    with pytest.raises(ValueError, match="DAILY_GM_BUDGET_REQUIRED"):
        LiveConfig(model_id="m", attempt_limit=10, attempt_period="utc_day", gm={"enabled": True})
    with pytest.raises(ValueError, match="DAILY_GM_BUDGET_REQUIRED"):
        LiveConfig(model_id="m", attempt_limit=300, gm={"enabled": True})  # lifetime period
    fits = LiveConfig(
        model_id="m",
        attempt_limit=22,
        attempt_period="utc_day",
        memory_processing=True,
        gm={"enabled": True},
    )
    assert fits.gm.daily_calls + fits.memory_daily_calls + fits.gm.dialogue_floor == 22
