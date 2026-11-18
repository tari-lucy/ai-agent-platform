#!/usr/bin/env python3
# ruff: noqa: E501  (the replacements quote lines of other files, they cannot be wrapped)
"""Собрать пакет building-kit для передачи: seeds навыка + файлы пакета, без привязок к платформе.

    python3 scripts/make_building_kit.py [--out /путь/building-kit] [--zip]

Источник правды один: скрипты берутся из seeds платформы (apps/core/.../local_host/seeds) и
подправляются при копировании; каждая замена обязана сработать, иначе сборка падает (так пакет не
расходится с навыком молча). Файлы, которые есть только в пакете, лежат в building-kit/ репозитория.
Репозиторий и навык платформы этот скрипт не меняет.
"""

import argparse
import importlib.util
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEEDS = ROOT / "apps/core/src/ai_agent_core/local_host/seeds"
STATIC = ROOT / "building-kit"
SCRIPTS = ("blueprint.py", "recipes.py", "design_check.py", "terrain.py", "build_plan.py")


def swap(text: str, old: str, new: str, where: str) -> str:
    if old not in text:
        raise SystemExit(f"замена не нашла своё место в {where}: {old[:70]!r}")
    return text.replace(old, new)


def adapt_build_plan(text: str) -> str:
    text = swap(text, "import time\n", "import time\n\nimport kit_env\n", "build_plan.py: импорт")
    text = swap(
        text, 'SHOTS = "/workspace/shots"', "SHOTS = kit_env.shots()", "build_plan.py: SHOTS"
    )
    text = swap(
        text,
        '    sys.path.insert(0, "/workspace/projects/unionclef")\n',
        "    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n",
        "build_plan.py: путь клиента",
    )
    text = swap(
        text,
        '    host = os.environ.get("AW_HOST_MINECRAFT_UNIONCLEF_MCP")\n    url = f"http://{host}/mcp" if host else m.DEFAULT_URL\n    return m.McpClient(url, m.DEFAULT_TOKEN)',
        "    return m.McpClient(m.DEFAULT_URL, m.DEFAULT_TOKEN)",
        "build_plan.py: адрес мода",
    )
    text = swap(
        text,
        'print(f"Посмотри: platform_look folder=shots photo={file} question=«{PICTURE_QUESTION}»")',
        'print(f"Посмотри: {kit_env.look_hint(os.path.join(SHOTS, file), file)}. Вопрос: «{PICTURE_QUESTION}»")',
        "build_plan.py: подсказка картинки",
    )
    text = swap(
        text,
        'print(f"Посмотри ({label}): platform_look folder=shots photo={file} question=«{PICTURE_QUESTION}»")',
        'print(f"Посмотри ({label}): {kit_env.look_hint(os.path.join(SHOTS, file), file)}. Вопрос: «{PICTURE_QUESTION}»")',
        "build_plan.py: подсказка кадра",
    )
    return swap(
        text,
        "(посмотри через platform_look, поправь схему)",
        "(посмотри картинку, поправь схему)",
        "build_plan.py: порядок",
    )


def paths(text: str) -> str:
    """Docstring paths of the platform's workspace become the kit's own folders."""
    text = text.replace("/workspace/skills/minecraft/", "scripts/").replace(
        "/workspace/skills/minecraft", "scripts"
    )
    text = (
        text.replace("/workspace/plans/", "plans/")
        .replace("/workspace/shots/", "shots/")
        .replace("/workspace/shots", "shots")
    )
    return text


def guide() -> str:
    spec = importlib.util.spec_from_file_location("building_guide", SEEDS / "building_guide.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    text = paths(module.BUILD_GUIDE)
    text = swap(
        text,
        "Платформа положила это один раз; дальше файл твой, дополняй своими находками.",
        "Это инструкция к пакету building-kit; дополняй её своими находками.",
        "guide: вступление",
    )
    text = swap(
        text,
        "Открой её через `platform_look` и честно найди",
        "Открой её своим средством зрения и честно найди",
        "guide: picture",
    )
    text = swap(
        text,
        "Посмотри каждый через `platform_look`, составь",
        "Посмотри каждый своим средством зрения, составь",
        "guide: review",
    )
    text = swap(
        text,
        "8. **Запиши в playbook/building.md**, что получилось и что нет: следующий раз начнёшь с удачного.",
        "8. **Запиши в свои заметки**, что получилось и что нет: следующий раз начнёшь с удачного.",
        "guide: заметки",
    )
    if re.search(r"platform|playbook|/workspace", text):
        raise SystemExit("в инструкции остались привязки к платформе")
    return text + (
        "\n## Как запускать\n"
        "Все команды запускаются из папки пакета: `python3 scripts/build_plan.py <команда> ...`. Адрес и токен мода, "
        "папка картинок лежат в `kit.env`. Перед работой запусти `python3 scripts/check_setup.py`: он скажет, "
        "отвечает ли мод и каких инструментов не хватает.\n"
    )


def example(out: Path) -> None:
    sys.path.insert(0, str(out / "scripts"))
    import blueprint
    import design_check
    import recipes

    target = out / "examples" / "gazebo"
    target.mkdir(parents=True, exist_ok=True)
    plan = recipes.gazebo(r=4, columns=6)
    plan.save(str(target / "plan.txt"))
    (target / "gazebo.py").write_text(
        '"""Беседка из готовой заготовки; запусти из папки пакета: python3 examples/gazebo/gazebo.py"""\n'
        "import sys\nfrom pathlib import Path\n\n"
        'sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))\n'
        "import recipes\n\n"
        "plan = recipes.gazebo(r=4, columns=6)\n"
        'plan.save(str(Path(__file__).with_name("plan.txt")))\n'
        "print(plan.report())\n",
        encoding="utf-8",
    )
    cells = {p: n for p, n in plan.cells.items()}
    design_check.save_views(cells, str(target / "plan-picture.png"))
    (target / "README.md").write_text(
        "# Пример: беседка\n\n- `gazebo.py` собирает её из заготовки (`recipes.gazebo`), `plan.txt` схема по слоям, "
        "`plan-picture.png` картинка чертежа с четырёх сторон.\n"
        "- Проверь: `python3 scripts/build_plan.py lint examples/gazebo/plan.txt`\n"
        "- Построй (с заглушкой или в игре): "
        "`python3 scripts/build_plan.py build examples/gazebo/plan.txt --origin X,Y,Z`\n",
        encoding="utf-8",
    )
    sys.path.remove(str(out / "scripts"))
    del blueprint


def build(out: Path, make_zip: bool) -> Path:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(STATIC, out, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in SCRIPTS:
        text = (SEEDS / name).read_text(encoding="utf-8")
        text = adapt_build_plan(text) if name == "build_plan.py" else text
        text = paths(text)
        if re.search(r"/workspace|platform_look|AW_HOST", text):
            raise SystemExit(f"в {name} остались привязки к платформе")
        (out / "scripts" / name).write_text(text, encoding="utf-8")
        if text.startswith("#!"):
            (out / "scripts" / name).chmod(0o755)  # a script with a shebang is run directly
    (out / "GUIDE_FOR_THE_MODEL.md").write_text(guide(), encoding="utf-8")
    shutil.copyfile(ROOT / "LICENSE", out / "LICENSE")
    example(out)
    if make_zip:
        archive = out.with_suffix(".zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(out.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    bundle.write(path, Path(out.name) / path.relative_to(out))
        return archive
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, default=ROOT.parent / "building-kit")
    parser.add_argument("--zip", action="store_true")
    args = parser.parse_args()
    print(build(args.out.resolve(), args.zip))
    return 0


if __name__ == "__main__":
    sys.exit(main())
