"""Granted host connections get stable variable names; the persona is told where she lives."""

from types import SimpleNamespace

from ai_agent_core.m3_composition.harness_host import (
    HarnessHost,
    HarnessSettings,
    host_port_env,
    host_port_names,
)


def test_each_granted_connection_gets_a_stable_variable_name_and_the_current_address():
    ports = {"Minecraft unionclef MCP": 25350, "second one": 9000}
    assert host_port_names(ports) == {
        "Minecraft unionclef MCP": "AW_HOST_MINECRAFT_UNIONCLEF_MCP",
        "second one": "AW_HOST_SECOND_ONE",
    }
    assert host_port_env(ports, "172.19.0.3") == {
        "AW_HOST_MINECRAFT_UNIONCLEF_MCP": "172.19.0.3:25350",
        "AW_HOST_SECOND_ONE": "172.19.0.3:9000",
    }
    assert host_port_env({}, "172.19.0.3") == {}


def test_the_prompt_says_where_she_lives_and_names_the_variables(tmp_path):
    settings = HarnessSettings(
        image_id="i",
        port=4096,
        password="p",
        upstream="https://x/v1",
        key="k",
        model="m",
        usage_dir=tmp_path,
        daily_budget=1.0,
        input_rate=0.1,
        output_rate=0.1,
        host_ports={"Minecraft unionclef MCP": 25350},
    )
    host = HarnessHost.__new__(HarnessHost)
    host.settings = settings
    scope = SimpleNamespace(persona_id="p")
    host.rt = SimpleNamespace(supervisor=SimpleNamespace(snapshot=SimpleNamespace(scope=scope)))
    config = host.config("http://172.19.0.3:8080/v1", "base", True)
    prompt = config["agent"]["p"]["prompt"] if "p" in config["agent"] else str(config)
    assert "not on the operator's computer" in prompt and "192.168" in prompt
    assert "172.19.0.3:25350 (environment variable AW_HOST_MINECRAFT_UNIONCLEF_MCP)" in prompt
    assert "read the address from the named environment variable" in prompt
    assert "Safety in the game" in prompt and "getOpenScreen" in prompt


def test_opencode_s_own_helpers_carry_the_persona_core_and_a_role_line(tmp_path):
    settings = HarnessSettings(
        image_id="i",
        port=4096,
        password="p",
        upstream="https://x/v1",
        key="k",
        model="m",
        usage_dir=tmp_path,
        daily_budget=1.0,
        input_rate=0.1,
        output_rate=0.1,
    )
    host = HarnessHost.__new__(HarnessHost)
    host.settings = settings
    scope = SimpleNamespace(persona_id="p")
    host.rt = SimpleNamespace(supervisor=SimpleNamespace(snapshot=SimpleNamespace(scope=scope)))
    full = "Ты — ЭТАЛОН, говоришь так и так.\n\n## Рабочее место\nправила работы\n"
    agents = host.config("http://x:8080/v1", full, False)["agent"]
    for helper in ("explore", "general", "compaction"):
        text = agents[helper]["prompt"]
        assert text.startswith("Ты — ЭТАЛОН, говоришь так и так.") and "## This role" in text
        assert "правила работы" not in text  # the workplace rules stay with her own agent
        assert set(agents[helper]) == {"prompt"}  # mode, tools and permissions of the built-in stay
    assert "first person" in agents["compaction"]["prompt"]
    assert agents["p"]["mode"] == "primary" and "правила работы" in agents["p"]["prompt"]


def test_the_native_chatgpt_trial_only_adds_the_openai_provider_to_the_list(tmp_path):
    def build(native):
        settings = HarnessSettings(
            image_id="i",
            port=4096,
            password="p",
            upstream="https://x/v1",
            key="k",
            model="m",
            usage_dir=tmp_path,
            daily_budget=1.0,
            input_rate=0.1,
            output_rate=0.1,
            native_chatgpt=native,
        )
        host = HarnessHost.__new__(HarnessHost)
        host.settings = settings
        scope = SimpleNamespace(persona_id="p")
        host.rt = SimpleNamespace(supervisor=SimpleNamespace(snapshot=SimpleNamespace(scope=scope)))
        return host.config("http://x:8080/v1", "base", False)

    assert build(False)["enabled_providers"] == ["platform"]
    on = build(True)
    assert on["enabled_providers"] == ["platform", "openai"]
    assert (
        on["model"] == "platform/bound" and on["small_model"] == "platform/bound"
    )  # her own work stays on the platform


def test_with_a_chosen_plan_model_everything_defaults_to_it_and_off_clears_it(tmp_path):
    def build(native, model):
        settings = HarnessSettings(
            image_id="i",
            port=4096,
            password="p",
            upstream="https://x/v1",
            key="k",
            model="m",
            usage_dir=tmp_path,
            daily_budget=1.0,
            input_rate=0.1,
            output_rate=0.1,
            native_chatgpt=native,
            native_chatgpt_model=model,
        )
        host = HarnessHost.__new__(HarnessHost)
        host.settings = settings
        scope = SimpleNamespace(persona_id="p")
        host.rt = SimpleNamespace(supervisor=SimpleNamespace(snapshot=SimpleNamespace(scope=scope)))
        return host.config("http://x:8080/v1", "base", False)

    on = build(True, "openai/gpt-5.1-codex")
    assert on["model"] == on["small_model"] == "openai/gpt-5.1-codex"
    # a model without the switch, or the switch without a model, changes nothing
    assert build(False, "openai/gpt-5.1-codex")["model"] == "platform/bound"
    assert build(True, None)["model"] == "platform/bound"


def test_a_repeated_call_is_refused_not_asked_about():
    """doom_loop would make OpenCode ask a human and wait: it must be a refusal."""
    import inspect

    from ai_agent_core.m3_composition import harness_host
    from ai_agent_core.m3_composition.harness_host import HarnessSettings  # noqa: F401

    source = inspect.getsource(harness_host)
    assert '"doom_loop": "deny"' in source
