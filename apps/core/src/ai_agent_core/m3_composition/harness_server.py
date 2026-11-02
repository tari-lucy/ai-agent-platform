"""Servers as a source of events: a new server is recognised as new, a known one as known.

The mod tells which server the client is on (address, list name, announced brand). The platform
looks for her profile of it in ``servers/``: by file name, by the address inside, by the header.
A server without a profile gets a skeleton (the facts the server itself gives, nothing else) and
she is told to learn it with RECON.md; a known one sends her to read its one file. So the same
steps work on any server, and what she learns stays in a file per server. Local data.
"""

import asyncio
import logging
import re
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ai_agent_contracts.common import Scope

from .harness_events import Event, Level

LOG = logging.getLogger(__name__)

MAX_PROFILES = 60
MAX_BYTES = 40_000

NEW_TEXT = (
    "New server: {title} ({address}). I created servers/{slug}.md with the basics the server "
    "gives. Learn the server now by skills/minecraft/RECON.md (menu, /warp or /warps, signs at the "
    "spawn, ask the players), fill the profile, then report from its main places."
)
KNOWN_TEXT = (
    "Known server: {title}. Read servers/{slug}.md (just that file) and continue from its "
    "places and notes."
)


def slug_of(address: str, name: str = "") -> str:
    host = address.split(":")[0].strip().lower() or name.strip().lower()
    host = re.sub(r"^(play|mc|server|www)\.", "", host)
    slug = re.sub(r"[^a-z0-9]+", "-", host).strip("-")
    return slug[:60] or "server"


def _plain(text: str) -> str:
    return re.sub(r"[^0-9a-zа-я]+", "", text.casefold())


def find_profile(folder: Path, address: str, name: str, brand: str) -> Path | None:
    """The profile of this server: file named by the address, else a file that mentions the
    address, else one whose first line is the server's name or brand."""
    if not folder.is_dir():
        return None
    guess = folder / f"{slug_of(address, name)}.md"
    if guess.is_file() and not guess.is_symlink():
        return guess
    host = address.split(":")[0].strip().casefold()
    names = {_plain(n) for n in (name, brand) if n and len(_plain(n)) >= 3}
    for index, path in enumerate(sorted(folder.glob("*.md"))):
        if index >= MAX_PROFILES:
            break
        if path.name.casefold() == "readme.md" or path.is_symlink():
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        first = text.splitlines()[0] if text else ""
        if host and host in text.casefold():
            return path
        if _plain(first.lstrip("# ")) in names:
            return path
    return None


def skeleton(title: str, address: str, name: str, brand: str) -> str:
    facts = ", ".join(
        part
        for part in (
            f"адрес {address}" if address else "",
            f"в списке: {name}" if name else "",
            f"бренд: {brand}" if brand else "",
        )
        if part
    )
    return (
        f"# {title}\n"
        f"Как узнать: {facts}.\n"
        "Тип: (выживание / мини-игры / креатив / хаб: определить при знакомстве)\n"
        "Измерение и спавн: \n"
        "Главные места: \n"
        "Команды: \n"
        "Правила и запреты: (проверить про ботов)\n"
        "Опасно: \n"
        "Чат и сленг: \n"
        "Люди: \n"
        "Что уже снято и опубликовано: \n"
        "Что проверить в следующий раз: \n"
    )


class ServerSource:
    name = "server"

    def __init__(self, game: Any, folder: Path, root: Path, interval: float = 20.0) -> None:
        self.game, self.folder, self.root, self.interval = game, folder, root, interval
        self.key: tuple[str, str, str] | None = None

    async def poll(self, scope: Scope, current: Callable[[], None]) -> list[Event]:
        if not self.game.in_game:
            self.key = None  # leaving the world: the next entry is looked at again
            return []
        try:
            info = await asyncio.to_thread(self.game.fetch, "getServerInfo", {})
        except OSError, ValueError, urllib.error.URLError:
            return []
        if not isinstance(info, dict) or not info.get("ok") or info.get("singleplayer"):
            return []  # an older mod without the tool, or a single-player world
        address = str(info.get("address") or "")[:100]
        name = str(info.get("name") or "")[:60]
        brand = str(info.get("brand") or "")[:60]
        if not (address or name or brand):
            return []
        key = (address, name, brand)
        if key == self.key:
            return []
        self.key = key
        current()
        title = name or brand or address
        if not self.root.resolve().is_dir() or not self.folder.resolve().is_relative_to(
            self.root.resolve()
        ):
            return []
        self.folder.mkdir(exist_ok=True)
        profile = find_profile(self.folder, address, name, brand)
        if profile is not None:
            self._remember_address(profile, address)
            slug = profile.stem
            return [
                Event(
                    "server", "known", slug, KNOWN_TEXT.format(title=title, slug=slug), Level.NORMAL
                )
            ]
        slug = slug_of(address, name or brand)
        path = self.folder / f"{slug}.md"
        if not path.exists():
            path.write_text(skeleton(title, address, name, brand), encoding="utf-8")
        text = NEW_TEXT.format(title=title, address=address or "?", slug=slug)
        return [Event("server", "new", slug, text, Level.HIGH)]

    @staticmethod
    def _remember_address(profile: Path, address: str) -> None:
        """A profile found by its name learns the address, so the next entry finds it at once."""
        host = address.split(":")[0].strip()
        if not host:
            return
        try:
            text = profile.read_text(encoding="utf-8")
            if host.casefold() not in text.casefold() and len(text) < MAX_BYTES:
                profile.write_text(
                    text.rstrip("\n") + f"\nАдрес (платформа): {host}\n", encoding="utf-8"
                )
        except OSError:
            return

    async def delivered(self, scope: Scope, current: Callable[[], None]) -> None:
        return None
