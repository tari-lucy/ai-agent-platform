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
    assert "не на компьютере оператора" in prompt and "192.168" in prompt
    assert "172.19.0.3:25350 (переменная окружения AW_HOST_MINECRAFT_UNIONCLEF_MCP)" in prompt
    assert "читай адрес из указанной переменной" in prompt
