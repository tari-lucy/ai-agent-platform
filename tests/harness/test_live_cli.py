"""The command line reaches `configure` with exactly the settings the flags name (doc37)."""

from pathlib import Path

from ai_agent_core.local_host import cli, harness
from ai_agent_core.local_host.layout import HarnessConfig


def run_configure(monkeypatch, tmp_path, *flags):
    captured: dict = {}

    def fake(root, **kwargs):
        captured.update(kwargs)
        return HarnessConfig()

    monkeypatch.setattr(harness, "configure", fake)
    monkeypatch.setattr(harness, "password_file", lambda host: tmp_path / "pw")
    assert cli.main(["configure-harness", "--root", str(tmp_path), *flags]) == 0
    return captured


def test_every_live_flag_is_passed_by_name(monkeypatch, tmp_path):
    got = run_configure(
        monkeypatch, tmp_path,
        "--avatar-port", "8001", "--avatar-profile", "opechatka.json",
        "--speech-voice", "Kayla", "--speech-port", "8003", "--speech-daily-chars", "5000",
        "--speech-hour-rate", "0.12", "--speech-mouth-delay", "0.3",
        "--listen-port", "8004", "--listen-names", "Печатка, Опечатка ",
        "--listen-max-minutes", "90",
        "--obs-port", "4455",
        "--chat-twitch", "moy_kanal", "--chat-youtube", "abc123", "--chat-daily-messages", "100",
    )  # fmt: skip
    assert got["avatar_port"] == 8001 and got["avatar_profile"] == "opechatka.json"
    assert (got["speech_voice"], got["speech_port"]) == ("Kayla", 8003)
    assert got["speech_daily_chars"] == 5000 and got["speech_hour_rate"] == 0.12
    assert got["speech_mouth_delay"] == 0.3
    assert got["listen_port"] == 8004 and got["listen_max_minutes"] == 90
    assert got["listen_names"] == ["Печатка", "Опечатка"]  # trimmed, no empty names
    assert got["obs_port"] == 4455
    assert (got["chat_twitch_channel"], got["chat_youtube_video"]) == ("moy_kanal", "abc123")
    assert got["chat_daily_messages"] == 100
    assert not any(got[k] for k in ("remove_avatar", "remove_speech", "remove_listen",
                                    "remove_obs", "remove_chats"))  # fmt: skip


def test_the_remove_flags_and_unset_flags(monkeypatch, tmp_path):
    got = run_configure(
        monkeypatch, tmp_path,
        "--remove-avatar", "--remove-speech", "--remove-listen", "--remove-obs", "--remove-chats",
    )  # fmt: skip
    assert all(got[k] for k in ("remove_avatar", "remove_speech", "remove_listen",
                                "remove_obs", "remove_chats"))  # fmt: skip
    for key in ("avatar_port", "speech_voice", "listen_port", "listen_names", "obs_port",
                "chat_twitch_channel", "chat_youtube_video"):  # fmt: skip
        assert got[key] is None  # unset means "keep what is there", never a change


def test_the_new_commands_reach_their_handlers(monkeypatch, tmp_path: Path):
    seen = {}
    from ai_agent_core.local_host import avatar_cli, people_cli

    monkeypatch.setattr(people_cli, "run", lambda root, action, args, yes: seen.update(
        people=(action, args, yes)) or 0)  # fmt: skip
    monkeypatch.setattr(avatar_cli, "run", lambda root, port, draft: seen.update(
        probe=(port, draft)) or 0)  # fmt: skip
    assert cli.main(["people", "link", "telegram:1", "twitch:2", "--root", str(tmp_path)]) == 0
    assert cli.main(["people", "erase", "p-1", "--root", str(tmp_path), "--yes"]) == 0
    assert cli.main(["people", "import-notes", "--root", str(tmp_path)]) == 0
    assert cli.main(["avatar-probe", "--root", str(tmp_path), "--draft", "a.json"]) == 0
    assert seen["probe"] == (8001, "a.json")
    assert seen["people"] == ("import-notes", [], False)
