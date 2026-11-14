"""The Minecraft start guide and orient.py: seeded once, never overwritten, valid Python."""

import py_compile

from ai_agent_core.local_host.harness import (
    MINECRAFT_GUIDE,
    ORIENT_SCRIPT,
    seed_minecraft_guide,
)


def test_the_guide_and_the_orientation_script_are_seeded_once(tmp_path):
    skills = tmp_path / "opechatka" / "skills" / "minecraft"
    skills.mkdir(parents=True)
    assert seed_minecraft_guide(tmp_path, "opechatka") is True
    guide = tmp_path / "opechatka" / "playbook" / "minecraft-start.md"
    orient = skills / "orient.py"
    assert guide.read_text(encoding="utf-8") == MINECRAFT_GUIDE
    assert orient.read_text(encoding="utf-8") == ORIENT_SCRIPT
    guide.write_text("my own version", encoding="utf-8")
    orient.write_text("# mine\n", encoding="utf-8")
    assert seed_minecraft_guide(tmp_path, "opechatka") is False  # hers now: never overwritten
    assert guide.read_text(encoding="utf-8") == "my own version"
    assert orient.read_text(encoding="utf-8") == "# mine\n"


def test_the_guide_names_the_one_way_to_speak_and_the_orientation():
    assert "platform_game_say" in MINECRAFT_GUIDE and "orient.py" in MINECRAFT_GUIDE
    assert "sendChatMessage не существует" in MINECRAFT_GUIDE
    assert "platform_voice_brief" in MINECRAFT_GUIDE


def test_orient_script_is_valid_python(tmp_path):
    path = tmp_path / "orient.py"
    path.write_text(ORIENT_SCRIPT, encoding="utf-8")
    py_compile.compile(str(path), doraise=True)
