"""M0-06 meaningful precedence/privacy/startup checks, synthetic bindings only."""

import json
import socket
import subprocess
from pathlib import Path

import pytest
from ai_agent_adapters.configuration import (
    ConfigurationError,
    fresh_startup,
    load_configuration,
    read_profile,
)

from ai_agent_core.bootstrap import create_app
from ai_agent_core.bootstrap import main as core_main
from ai_agent_worker.bootstrap import create_worker_bootstrap
from ai_agent_worker.bootstrap import main as worker_main

CANARY = "public-synthetic-binding-marker"
ENDPOINT = "https://synthetic.invalid/private-binding-path"


def profile_file(tmp_path, data, name="profile.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_precedence_and_explicit_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PLATFORM_WORKSPACE_ID", "host-must-not-leak")
    monkeypatch.setenv("AI_PLATFORM_EMBEDDING_BATCH", "999")
    profile = profile_file(
        tmp_path,
        {
            "schema_version": "1.0",
            "workspace_id": "profile",
            "persona_id": "persona-a",
            "memory_scope": "test",
            "embedding_batch": 2,
        },
    )
    override = profile_file(tmp_path, {"embedding_batch": 1}, "override.json")
    binding = tmp_path / "fixture.env"
    binding.write_text("AI_PLATFORM_WORKSPACE_ID=local-env\nAI_PLATFORM_EMBEDDING_BATCH=3\n")
    settings = load_configuration(
        profile_path=profile,
        overrides_path=override,
        env_file=binding,
        environ={"AI_PLATFORM_EMBEDDING_BATCH": "4"},
    )
    assert settings.workspace_id == "local-env"
    assert settings.persona_id == "persona-a" and settings.memory_scope == "test"
    assert settings.embedding_batch == 4
    assert load_configuration(environ={}).workspace_id == "default"


@pytest.mark.parametrize(
    "payload,field,code",
    [
        ({"schema_version": "2.0"}, "schema_version", "unsupported_value"),
        ({}, "schema_version", "required"),
        ({"schema_version": "1.0", "autostart": True}, "autostart", "must_be_disabled"),
        ({"schema_version": "1.0", "memory_scope": "shared"}, "memory_scope", "unsupported_value"),
        ({"schema_version": "1.0", "embedding_batch": 5}, "embedding_batch", "above_maximum"),
        (
            {"schema_version": "1.0", "worker_memory_mib": 1537},
            "worker_memory_mib",
            "above_maximum",
        ),
        ({"schema_version": "1.0", "pending_jobs": 0}, "pending_jobs", "below_minimum"),
        ({"schema_version": "1.0", "core_host_port": 80}, "core_host_port", "below_minimum"),
        ({"schema_version": "1.0", "core_host_port": 65536}, "core_host_port", "above_maximum"),
        (
            {"schema_version": "1.0", "workspace_id": "../private"},
            "workspace_id",
            "invalid_identifier",
        ),
        ({"schema_version": "1.0", "core_host_port": CANARY}, "core_host_port", "integer_required"),
        ({"schema_version": "1.0", CANARY: CANARY}, "profile", "unknown_field"),
        ({"schema_version": "1.0", "llm_api_key": CANARY}, "profile", "unknown_field"),
    ],
)
def test_invalid_profile_explains_without_values(tmp_path, payload, field, code):
    path = profile_file(tmp_path, payload)
    with pytest.raises(ConfigurationError) as caught:
        load_configuration(profile_path=path, environ={})
    assert caught.value.field == field and caught.value.code == code
    assert CANARY not in str(caught.value) and str(path) not in str(caught.value)
    assert CANARY not in repr(caught.value)


def test_missing_malformed_and_large_profile(tmp_path):
    path = tmp_path / (CANARY + ".json")
    with pytest.raises(ConfigurationError, match="unreadable_file"):
        read_profile(path)
    path.write_text('{"' + CANARY)
    with pytest.raises(ConfigurationError, match="invalid_json"):
        read_profile(path)
    path.write_text("[" + " " * 262144 + "]")
    with pytest.raises(ConfigurationError, match="too_large"):
        read_profile(path)


@pytest.mark.parametrize(
    "env,field",
    [
        ({"AI_PLATFORM_REDIS_PASSWORD": CANARY}, "redis_password"),
        ({"AI_PLATFORM_LLM_API_KEY": CANARY}, "llm_binding"),
        ({"AI_PLATFORM_LLM_ENDPOINT": ENDPOINT}, "llm_binding"),
        ({"AI_PLATFORM_LLM_ENDPOINT": CANARY, "AI_PLATFORM_LLM_API_KEY": CANARY}, "llm_endpoint"),
        (
            {
                "AI_PLATFORM_LLM_ENDPOINT": "https://user:pass@synthetic.invalid",
                "AI_PLATFORM_LLM_API_KEY": CANARY,
            },
            "llm_endpoint",
        ),
        (
            {
                "AI_PLATFORM_LLM_ENDPOINT": "https://synthetic.invalid:99999",
                "AI_PLATFORM_LLM_API_KEY": CANARY,
            },
            "llm_endpoint",
        ),
        ({"AI_PLATFORM_REDIS_PASSWORD": "a" * 64, "REDIS_PASSWORD": "b" * 64}, "redis_password"),
        ({"AI_PLATFORM_AUTOSTART": "true"}, "autostart"),
    ],
)
def test_invalid_environment_is_safe(env, field):
    with pytest.raises(ConfigurationError) as caught:
        load_configuration(environ=env)
    assert caught.value.field == field
    assert CANARY not in str(caught.value) and ENDPOINT not in str(caught.value)
    assert "user:pass" not in str(caught.value)


def test_secret_repr_manifest_and_no_implicit_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("AI_PLATFORM_AUTOSTART=true\n")
    assert not load_configuration(environ={}).autostart  # cwd .env is never discovered
    settings = load_configuration(
        environ={
            "REDIS_PASSWORD": "a" * 64,
            "AI_PLATFORM_LLM_ENDPOINT": ENDPOINT,
            "AI_PLATFORM_LLM_API_KEY": CANARY,
            "AI_PLATFORM_AUTOSTART": "false",
        }
    )
    assert CANARY not in repr(settings) and ENDPOINT not in repr(settings)
    assert CANARY not in settings.model_dump_json() and ENDPOINT not in settings.model_dump_json()
    manifest = fresh_startup().safe_manifest(settings)
    encoded = json.dumps(manifest)
    assert CANARY not in encoded and ENDPOINT not in encoded and "a" * 64 not in encoded
    assert "workspace_id" not in manifest and "persona_id" not in manifest
    assert manifest["llm_bound"] is True and manifest["bootstrap_state"] == "STOPPED"


def test_dotenv_does_not_expand_external_secret_references(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTERNAL_SENSITIVE_TEST_VALUE", CANARY)
    binding = tmp_path / "fixture.env"
    binding.write_text(
        "AI_PLATFORM_LLM_ENDPOINT="
        + ENDPOINT
        + "\n"
        + "AI_PLATFORM_LLM_API_KEY=${EXTERNAL_SENSITIVE_TEST_VALUE}\n"
    )
    settings = load_configuration(env_file=binding, environ={})
    assert settings.llm_api_key.get_secret_value() == "${EXTERNAL_SENSITIVE_TEST_VALUE}"


@pytest.mark.parametrize("previous", ["ACTIVE", "STARTING", "STOPPING", "FAULTED", None])
def test_restart_restore_never_activate_or_reuse_epoch(previous):
    settings = load_configuration(environ={})
    app = create_app(settings, previous_state=previous)
    worker = create_worker_bootstrap(settings, previous_state=previous)
    assert app.state.bootstrap.state == worker.state == "STOPPED"
    assert app.state.bootstrap.effects_enabled is worker.effects_enabled is False
    assert app.state.bootstrap.epoch != worker.epoch
    assert app.routes == [] and app.router.on_startup == []


def test_cli_validation_happens_without_external_effects(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("bootstrap attempted an external effect")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setenv("AI_PLATFORM_LLM_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("AI_PLATFORM_LLM_API_KEY", CANARY)
    core_main()
    worker_main()
    captured = capsys.readouterr()
    assert "STOPPED" in captured.out
    assert CANARY not in captured.out + captured.err and ENDPOINT not in captured.out + captured.err


def test_cli_safe_error_exit_before_core_is_constructed(monkeypatch, capsys):
    monkeypatch.setenv("AI_PLATFORM_CORE_HOST_PORT", CANARY)
    for main in (core_main, worker_main):
        with pytest.raises(SystemExit) as caught:
            main()
        assert caught.value.code == 78
    captured = capsys.readouterr()
    assert "core_host_port: integer_required" in captured.out
    assert CANARY not in captured.out + captured.err


@pytest.mark.parametrize("entrypoint", ["core.py", "worker.py"])
def test_container_entrypoint_rejects_before_listener_or_heartbeat(monkeypatch, capsys, entrypoint):
    import runpy

    entry_dir = Path("/entrypoints")
    if not entry_dir.exists():
        entry_dir = Path(__file__).parents[3] / "deploy"
    monkeypatch.setenv("CORE_CONTAINER_PORT", CANARY)

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid config opened a listener/process")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(entry_dir / entrypoint), run_name="__main__")
    assert caught.value.code == 78
    captured = capsys.readouterr()
    assert "core_container_port: integer_required" in captured.err
    assert CANARY not in captured.out + captured.err
