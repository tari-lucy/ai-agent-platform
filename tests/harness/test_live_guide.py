"""The live memo is seeded once and never overwritten; the prompt mentions only what exists."""

from types import SimpleNamespace

from ai_agent_core.local_host.harness import LIVE_GUIDE, seed_live_guide
from ai_agent_core.m3_composition.harness_host import HarnessHost, HarnessSettings


def test_the_memo_is_written_once_and_then_belongs_to_her(tmp_path):
    assert seed_live_guide(tmp_path, "alpha") is True
    target = tmp_path / "alpha" / "playbook" / "live.md"
    assert target.read_text(encoding="utf-8") == LIVE_GUIDE
    target.write_text("мои правки", encoding="utf-8")
    assert seed_live_guide(tmp_path, "alpha") is False  # again: nothing is touched
    assert target.read_text(encoding="utf-8") == "мои правки"
    assert seed_live_guide(None, "alpha") is False


def test_a_linked_folder_or_file_is_never_followed(tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "beta" / "playbook").symlink_to(real)
    assert seed_live_guide(tmp_path, "beta") is False
    assert list(real.iterdir()) == []
    (tmp_path / "gamma" / "playbook").mkdir(parents=True)
    (tmp_path / "gamma" / "playbook" / "live.md").symlink_to(real / "x.md")
    assert seed_live_guide(tmp_path, "gamma") is False and not (real / "x.md").exists()


def test_every_tool_the_memo_names_exists():
    from ai_agent_core.m3_composition import harness_mcp as mcp

    names = {
        t["name"]
        for group in (
            [mcp.AVATAR_TOOL, mcp.SPEAK_TOOL, mcp.LISTEN_TOOL],
            mcp.STAGE_TOOLS,
            mcp.CHAT_TOOLS,
            mcp.PEOPLE_TOOLS,
        )
        for t in group
    }
    for word in ("avatar_express", "speak", "listen", "chat_read", "chat_say", "stream_status",
                 "stream_scene", "person_find", "person_recall", "person_note", "person_met",
                 "person_link", "ticket_raise"):  # fmt: skip
        assert word in LIVE_GUIDE
        assert word in names or word == "ticket_raise"


def prompt(tmp_path, **parts):
    settings = HarnessSettings(
        image_id="i", port=4096, password="p", upstream="https://x/v1", key="k", model="m",
        usage_dir=tmp_path, daily_budget=1.0, input_rate=0.1, output_rate=0.1,
    )  # fmt: skip
    host = HarnessHost.__new__(HarnessHost)
    host.settings = settings
    scope = SimpleNamespace(persona_id="p")
    host.rt = SimpleNamespace(supervisor=SimpleNamespace(snapshot=SimpleNamespace(scope=scope)))
    for key, value in parts.items():
        setattr(host, key, value)
    return str(host.config("http://x:8080/v1", "base", False))


def test_the_prompt_names_only_the_live_abilities_that_exist(tmp_path):
    assert "## Live" not in prompt(tmp_path)
    text = prompt(tmp_path, avatar=object(), chats=object())
    assert "## Live" in text and "avatar_express" in text and "chat_read/chat_say" in text
    assert "speak (aloud)" not in text and "OBS" not in text
    assert "playbook/live.md" in text
    everything = prompt(tmp_path, avatar=1, speaker=1, listener=1, stage=1, chats=1)
    start = everything.index("## Live")
    end = everything.find("## ", start + 3)
    section = everything[start : end if end != -1 else None]
    assert 200 < len(section) < 1200  # the prompt stays small: details live in the memo
