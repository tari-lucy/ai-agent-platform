"""The building-kit package.

It is built from the platform's seeds, has nothing of the platform in it, and the whole chain runs
(site, level, stock, build, cleanup, review, dig) from a clean folder against the mock game.
"""

import ast
import hashlib
import importlib.util
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEEDS = ROOT / "apps/core/src/ai_agent_core/local_host/seeds"
MAKER = ROOT / "scripts" / "make_building_kit.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def maker():
    return load(MAKER, "make_building_kit")


@pytest.fixture(scope="module")
def kit(tmp_path_factory, maker):
    out = tmp_path_factory.mktemp("kit") / "building-kit"
    maker.build(out, make_zip=False)
    return out


def digest(folder: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.glob("*.py"))}


def test_the_kit_is_made_from_the_seeds_and_the_seeds_are_left_alone(tmp_path, maker):
    before = digest(SEEDS)
    maker.build(tmp_path / "building-kit", make_zip=False)
    assert digest(SEEDS) == before


def test_the_kit_has_everything_and_nothing_of_the_platform(kit):
    names = {p.name for p in (kit / "scripts").glob("*.py")}
    assert {
        "blueprint.py",
        "recipes.py",
        "design_check.py",
        "terrain.py",
        "build_plan.py",
        "mcp_client.py",
        "kit_env.py",
        "goto_wait.py",
        "shot.py",
        "check_setup.py",
        "mock_mcp.py",
    } <= names
    for name in ("README.md", "MOD.md", "GUIDE_FOR_THE_MODEL.md", "kit.env.example", "LICENSE"):
        assert (kit / name).is_file(), name
    assert (kit / "examples/gazebo/plan.txt").is_file() and (
        kit / "examples/gazebo/plan-picture.png"
    ).is_file()
    for path in kit.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".md", ".example", ".txt"}:
            text = path.read_text(encoding="utf-8")
            for leak in ("/workspace", "platform_look", "AW_HOST", "playbook/"):
                assert leak not in text, f"{leak} in {path.relative_to(kit)}"
            if path.parent.name == "scripts" or path.name == "GUIDE_FOR_THE_MODEL.md":
                assert "Опечатк" not in text, (
                    path.name
                )  # only the README says where the kit grew from


def test_the_scripts_are_plain_python_310(kit):
    for path in (kit / "scripts").glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 10))


def test_a_replacement_that_misses_its_place_stops_the_build(tmp_path, maker, monkeypatch):
    fake = tmp_path / "seeds"
    fake.mkdir()
    for name in maker.SCRIPTS + ("building_guide.py",):
        (fake / name).write_text((SEEDS / name).read_text(encoding="utf-8"), encoding="utf-8")
    text = (
        (fake / "build_plan.py").read_text(encoding="utf-8").replace("SHOTS = ", "PICTURES = ", 1)
    )
    (fake / "build_plan.py").write_text(text, encoding="utf-8")
    monkeypatch.setattr(maker, "SEEDS", fake)
    with pytest.raises(SystemExit, match="SHOTS"):
        maker.build(tmp_path / "out", make_zip=False)


def test_the_zip_holds_one_top_folder(tmp_path, maker):
    archive = maker.build(tmp_path / "building-kit", make_zip=True)
    names = zipfile.ZipFile(archive).namelist()
    assert archive.suffix == ".zip" and all(n.startswith("building-kit/") for n in names)
    assert "building-kit/scripts/build_plan.py" in names and not any(
        "__pycache__" in n for n in names
    )


@pytest.fixture()
def game(kit):
    mock = load(kit / "scripts" / "mock_mcp.py", "kit_mock_mcp")
    world = mock.World(hills=True, scaffold=True)
    server, _ = mock.serve(0, "SECRET", world)
    yield mock, world, server.server_address[1]
    server.shutdown()


def run(
    kit: Path, port: int, script: str, *args: str, token: str = "SECRET", env: dict | None = None
):
    environment = {
        **os.environ,
        "UNIONCLEF_MCP_URL": f"http://127.0.0.1:{port}/mcp",
        "UNIONCLEF_MCP_TOKEN": token,
        "BUILD_KIT_SHOTS": str(kit / "shots"),
        **(env or {}),
    }
    return subprocess.run(
        [sys.executable, str(kit / "scripts" / script), *args],
        cwd=kit,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_setup_check_says_what_works(kit, game):
    _, _, port = game
    done = run(kit, port, "check_setup.py")
    assert done.returncode == 0 and "Всё на месте" in done.stdout


def test_the_setup_check_names_the_missing_tools_of_an_old_mod(kit, game):
    mock, _, port = game
    full = list(mock.TOOLS)
    mock.TOOLS[:] = [
        t for t in full if t not in ("scanBox", "getHeightmap")
    ]  # what upstream UnionClef lacks
    try:
        done = run(kit, port, "check_setup.py")
    finally:
        mock.TOOLS[:] = full
    assert done.returncode == 2
    assert "НЕТ   site, level (карта высот): не хватает getHeightmap" in done.stdout
    assert "НЕТ   cleanup (уборка лесов по миру): не хватает scanBox" in done.stdout


def test_no_game_and_a_wrong_token_are_said_plainly(kit, game):
    _, _, port = game
    assert "проверь токен" in run(kit, port, "check_setup.py", token="WRONG").stdout
    away = run(kit, 9, "check_setup.py")
    assert away.returncode == 1 and "Нет связи с модом" in away.stdout


def test_the_settings_can_come_from_kit_env(kit, game):
    _, _, port = game
    (kit / "kit.env").write_text(
        f"UNIONCLEF_MCP_URL=http://127.0.0.1:{port}/mcp\nUNIONCLEF_MCP_TOKEN=SECRET\n"
    )
    try:
        bare = {k: v for k, v in os.environ.items() if not k.startswith("UNIONCLEF_")}
        done = subprocess.run(
            [sys.executable, str(kit / "scripts" / "check_setup.py")],
            cwd=kit,
            env=bare,
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        (kit / "kit.env").unlink()
    assert done.returncode == 0, done.stdout


def test_the_whole_chain_runs_from_a_clean_folder(kit, game, tmp_path):
    _, world, port = game
    plan = "examples/gazebo/plan.txt"
    origin = ("--origin", "9,68,9")  # on the mock's hill
    sites = run(kit, port, "build_plan.py", "site", "7x9", "--near", "10,10")
    assert sites.returncode == 0 and "--origin" in sites.stdout
    lint = run(kit, port, "build_plan.py", "lint", plan)
    assert lint.returncode == 0 and "[замечание]" in lint.stdout  # notes, no errors
    picture = run(kit, port, "build_plan.py", "picture", plan, "--name", "demo")
    assert (
        picture.returncode == 0 and (kit / "shots" / "demo-plan.png").read_bytes()[:4] == b"\x89PNG"
    )
    leveled = run(kit, port, "build_plan.py", "level", plan, *origin, "--mode", "commands")
    assert leveled.returncode == 0 and "ГОТОВО" in leveled.stdout
    assert (
        run(kit, port, "build_plan.py", "build", plan, *origin).returncode == 6
    )  # no materials yet: it will not build blind
    assert run(kit, port, "build_plan.py", "stock", plan, *origin).returncode == 0
    built = run(kit, port, "build_plan.py", "build", plan, *origin)
    assert built.returncode == 0, built.stdout
    assert "Постройка чистая и совпадает с планом" in built.stdout and not world.scaffold
    assert run(kit, port, "build_plan.py", "verify", plan, *origin).stdout.strip() == "ВСЁ НА МЕСТЕ"
    world.cells[(7, 70, 7)] = "minecraft:oak_planks"  # a stray block next to the build
    cleaned = run(kit, port, "build_plan.py", "cleanup", plan, *origin)
    assert cleaned.returncode == 0 and "Лишнее вокруг постройки" in cleaned.stdout
    assert (7, 70, 7) not in world.cells
    review = run(kit, port, "build_plan.py", "review", plan, *origin, "--name", "demo")
    assert review.returncode == 0 and sorted(
        p.name for p in (kit / "shots").glob("demo-?.jpg")
    ) == ["demo-1.jpg", "demo-2.jpg", "demo-3.jpg", "demo-4.jpg"]
    assert "своим средством зрения" in review.stdout  # the look hint, not a platform tool


def test_a_dig_plan_refuses_water_and_a_look_hint_can_be_replaced(kit, game):
    _, world, port = game
    dig = kit / "dig.txt"
    dig.write_text("легенда:\nX = выкопать\n\nслой 0\nXX\nXX\n", encoding="utf-8")
    world.cells[(31, 64, 30)] = "minecraft:gravel"  # loose gravel beside the pit
    refused = run(kit, port, "build_plan.py", "dig", str(dig), "--origin", "30,63,30")
    assert refused.returncode == 12 and "ОПАСНО" in refused.stdout
    shown = run(
        kit,
        port,
        "build_plan.py",
        "picture",
        "examples/gazebo/plan.txt",
        env={"BUILD_KIT_LOOK_HINT": "vision.open({file})"},
    )
    assert "vision.open(plan-plan.png)" in shown.stdout
