"""The builders that turn settings and secrets into live owners (doc37): off without their
inputs, on with them, and a bad input never stops the host."""

import json
from pathlib import Path
from types import SimpleNamespace

from ai_agent_core.local_host.harness import avatar, chats, listener, speaker, stage
from ai_agent_core.local_host.layout import HarnessConfig, HostPaths

SCOPE = SimpleNamespace(persona_id="alpha", partition="p")
PROFILE = {"version": 1, "emotions": {"happy": {"hotkey": "Smile"}}, "mouth_parameter": "MouthOpen"}
SONIOX = {"AI_PLATFORM_SONIOX_API_KEY": "synthetic-soniox"}
TWITCH = {"AI_PLATFORM_TWITCH_TOKEN": "t", "AI_PLATFORM_TWITCH_NICK": "opechatka"}
YOUTUBE = {
    "AI_PLATFORM_YOUTUBE_CLIENT_ID": "id",
    "AI_PLATFORM_YOUTUBE_CLIENT_SECRET": "s",
    "AI_PLATFORM_YOUTUBE_REFRESH_TOKEN": "r",
}


def host(tmp_path: Path) -> HostPaths:
    return HostPaths(tmp_path)


def providers(secrets=None, binding=("https://x/v1", "k")):
    return SimpleNamespace(secrets=dict(secrets or {}), binding=binding)


def usage():
    return SimpleNamespace(remaining=lambda: None, record_amount=lambda *a, **k: None)


def test_avatar_needs_port_and_a_readable_profile(tmp_path):
    folder = tmp_path / "data" / "harness" / "avatar"
    folder.mkdir(parents=True)
    assert avatar(HarnessConfig(), host(tmp_path)) is None
    settings = HarnessConfig(avatar_port=8001, avatar_profile="a.json")
    assert avatar(settings, host(tmp_path)) is None  # no profile file: off, no crash
    (folder / "a.json").write_text("{not json")
    assert avatar(settings, host(tmp_path)) is None  # a broken profile: off, no crash
    (folder / "a.json").write_text(json.dumps(PROFILE))
    built = avatar(settings, host(tmp_path))
    assert built is not None and built.state == "idle"  # not tried yet: not an error


def test_speech_needs_voice_port_and_the_soniox_key(tmp_path):
    settings = HarnessConfig(speech_voice="Kayla", speech_port=8003, speech_hour_rate=0.12)
    assert speaker(HarnessConfig(), host(tmp_path), providers(SONIOX), usage(), None) is None
    assert speaker(settings, host(tmp_path), providers(), usage(), None) is None  # no key
    built = speaker(settings, host(tmp_path), providers(SONIOX), usage(), None)
    assert built is not None and built.rate == 24000 and built.hour_rate == 0.12
    assert built.mouth_delay == 0.15
    assert built.journal == tmp_path / "data" / "harness" / "speech.jsonl"


def test_listening_names_include_hers_and_it_needs_the_key(tmp_path):
    settings = HarnessConfig(
        listen_port=8004, listen_names=["Печатка"], agent_names={"alpha": "Опечатка"}
    )
    assert listener(HarnessConfig(), host(tmp_path), providers(SONIOX), usage(), SCOPE) is None
    assert listener(settings, host(tmp_path), providers(), usage(), SCOPE) is None
    built = listener(settings, host(tmp_path), providers(SONIOX), usage(), SCOPE)
    assert built is not None and {"печатка", "опечатка", "alpha"} <= set(built.names)
    assert built.max_minutes == 180 and not built.on


def test_obs_is_off_without_a_port_and_the_password_is_optional():
    assert stage(HarnessConfig(), providers()) is None
    assert stage(HarnessConfig(obs_port=4455), providers()).state == "idle"  # type: ignore[union-attr]
    assert stage(HarnessConfig(obs_port=4455), providers({"AI_PLATFORM_OBS_PASSWORD": "p"}))


def test_chats_join_only_the_platforms_that_have_channel_and_secrets(tmp_path):
    base = {"model": "m", "scope": SCOPE}
    none = chats(HarnessConfig(), host(tmp_path), providers(TWITCH), usage(), **base)
    assert none is None
    only_twitch = chats(
        HarnessConfig(chat_twitch_channel="moy_kanal", chat_youtube_video="abc123"),
        host(tmp_path),
        providers(TWITCH),
        usage(),
        **base,
    )
    assert only_twitch is not None and set(only_twitch.platforms) == {"twitch"}
    both = chats(
        HarnessConfig(chat_twitch_channel="moy_kanal", chat_youtube_video="abc123"),
        host(tmp_path),
        providers({**TWITCH, **YOUTUBE}),
        usage(),
        **base,
    )
    assert both is not None and set(both.platforms) == {"twitch", "youtube"}
    assert both.own == {"twitch": "opechatka"}
    no_model = chats(
        HarnessConfig(chat_twitch_channel="moy_kanal"),
        host(tmp_path),
        providers(TWITCH, binding=None),
        usage(),
        **base,
    )
    assert no_model is None  # the public-line check needs the model binding
