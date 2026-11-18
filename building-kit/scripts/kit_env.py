"""Настройки пакета: адрес и токен мода, папка картинок. Читает kit.env (рядом с папкой scripts).

Порядок: переменные окружения > файл kit.env > значения по умолчанию. Только стандартная библиотека.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_LOADED = False


def load() -> None:
    """Подмешать kit.env в окружение, не затирая то, что уже задано снаружи."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    path = ROOT / "kit.env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def url() -> str:
    load()
    return os.environ.get("UNIONCLEF_MCP_URL", "http://127.0.0.1:25350/mcp")


def token() -> str:
    load()
    return os.environ.get("UNIONCLEF_MCP_TOKEN", "")


def shots() -> str:
    load()
    folder = Path(os.environ.get("BUILD_KIT_SHOTS", "shots"))
    return str(folder if folder.is_absolute() else ROOT / folder)


def look_hint(path: str, file: str) -> str:
    """Фраза «посмотри картинку» для модели; свою формулировку задают в BUILD_KIT_LOOK_HINT."""
    load()
    template = os.environ.get(
        "BUILD_KIT_LOOK_HINT",
        "Открой изображение {path} своим средством зрения и ответь на вопрос ниже",
    )
    return template.format(path=path, file=file)
