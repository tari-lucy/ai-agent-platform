"""The game as a source of events: read the mod's state (no model) and report what changed.

The platform reaches the mod on the granted host port (the tunnel puts it on this machine's
loopback) with the same token the mod gave her; the token is read from her workspace file at
each poll and never logged. What it sees: she is hurt or low on health, probably died, a player
came near, someone said her name, hunger. The mod's state is read-only data; texts that come
from other players are untrusted and stay marked as data in the digest.
"""

import asyncio
import json
import logging
import math
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ai_agent_contracts.common import Scope

from .harness_events import Event, Focus, Level
from .harness_stance import Action, Hostile, Reflex

LOG = logging.getLogger(__name__)

NEAR = 15.0  # blocks: a player closer than this is "near" (a lobby is full of players further off)
NEAR_AGAIN = 600.0  # seconds before the same player is reported near again
MOB_NEAR = 8.0  # blocks: a hostile mob closer than this is a threat worth a line
STUCK_WINDOW = 45.0  # seconds of trying to walk without getting anywhere = stuck
STUCK_MOVE = 2.0  # blocks: moving less than this over the window means not moving
STUCK_AGAIN = 120.0  # seconds before the stuck news repeats
SCREEN_WINDOW = 45.0  # seconds an open screen is left before she is reminded it blocks walking
SCREEN_AGAIN = 180.0
# Applied once on entering a world: the pathfinder must not place or break blocks by itself.
# Lobbies and the End look "closed" to it and it bridges and pillars forever; she can turn a
# setting on for a job with ExecuteCommand and the platform sets it again at the next entry.
JOIN_SETTINGS = ("allowPlace", "allowBreak")
BOTS = frozenset({"alto clef", "altoclef", "alto", "clef", "tungsten", "server", "system", "console"})
TALK_WINDOW = 120.0  # seconds a player who spoke to her is followed after his last line
SENDER = re.compile(r"(?:<([A-Za-z0-9_]{3,16})>|([A-Za-z0-9_]{3,16})\s*[\u00bb>:])")
MOB_PAUSE = 60  # polls to wait before asking again when the mod has no such tool
LOW_HP = 0.3  # of the maximum
CHAT_LINES = 40
MAX_TOKEN = 400
SECTION = re.compile("§.")  # Minecraft colour codes


def mcp_call(url: str, token: str, name: str, arguments: dict[str, Any], timeout: float) -> Any:
    """One MCP tool call; the tool's text result parsed as JSON when it is JSON."""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    ).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        message = json.loads(response.read(2 * 1024 * 1024) or b"null")
    result = (message or {}).get("result") or {}
    content = result.get("content") or []
    text = content[0].get("text", "") if content else ""
    try:
        return json.loads(text)
    except ValueError:
        return text


def read_token(path: Path, root: Path | None = None) -> str | None:
    """The mod's token from her workspace: a plain small file, never a link, and (when ``root``
    is given) really inside her folder: a symlinked directory cannot point the host elsewhere."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_TOKEN:
            return None
        if root is not None and not path.resolve().is_relative_to(root.resolve()):
            return None
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def _floats(text: Any) -> tuple[float, float, float] | None:
    try:
        x, y, z = (float(part) for part in str(text).split(","))
    except ValueError:
        return None
    return x, y, z


class GameSource:
    name = "game"

    def __init__(
        self,
        url: str,
        token_path: Path,
        names: list[str],
        interval: float = 5.0,
        call: Callable[[str, dict[str, Any]], Any] | None = None,
        root: Path | None = None,
        reflex: Reflex | None = None,
        clock: Callable[[], float] | None = None,
        people: Path | None = None,
        book: Any = None,
    ) -> None:
        self.people = people  # her notes about players: name and a fact per line
        self.book = book  # the cross-platform book of people (harness_people), or None
        self._talk: dict[str, float] = {}
        # Her in-game nicknames (her own lines carry them, not her name). Learned: a chat line that
        # ends with what she just sent is hers; or told by the host (her notes name the bot).
        self.own_nicks: set[str] = set()
        self._sent: list[tuple[str, float]] = []
        # Set by the host: a fast voice that answers players in the chat at once. It gets the line
        # and what is known of the player; the slow mind is woken only by its hand-overs.
        self.on_talk: Callable[[str, str], None] | None = None
        self.reflex = reflex
        self.clock = clock or time.monotonic
        self._alive = True
        self._dying = False  # health was low: a vanishing from the world is probably a death
        self._gone_since: float | None = None
        self._dead_told = False
        self.url, self.token_path, self.interval, self.root = url, token_path, interval, root
        self.names = [n.casefold() for n in names if n]
        self._call = call
        self.in_game = False
        self.reachable = False
        self.state: dict[str, Any] = {}
        self._prev: dict[str, Any] | None = None
        self._seen_chat: list[str] = []
        self._near: set[str] = set()
        self._near_told: dict[str, float] = {}
        self._low_hp = False
        self._low_food = False
        self._mobs: set[str] = set()
        self._mob_pause = 0
        self._set_up = False
        self._screen_since: float | None = None
        self._screen_told = -1e9
        self._walk: list[tuple[float, tuple[float, float, float]]] = []
        self._stuck_told = -1e9

    def sent(self, text: str) -> None:
        """She just wrote ``text`` in the chat (the host tells): her own line will show up in it."""
        now = self.clock()
        self._sent = [(t, at) for t, at in self._sent if now - at < 120.0][-20:]
        self._sent.append((" ".join(text.split()).casefold(), now))

    def _mine(self, line: str, sender: str | None) -> bool:
        """Is this chat line her own? By a nick she is known by, or by what she just sent."""
        if sender is not None and sender.casefold() in self.own_nicks:
            return True
        flat = " ".join(line.split()).casefold()
        for text, _at in self._sent:
            if len(text) >= 8 and flat.endswith(text[-60:]):
                if sender is not None:
                    self.own_nicks.add(sender.casefold())  # learned: the nick of her bot
                return True
        return False

    def fetch(self, tool: str, arguments: dict[str, Any]) -> Any:
        """One tool call for other sources (they run it in a thread): the mod's answer, parsed."""
        return self._fetch(tool, arguments)

    def focus(self) -> Focus:
        return "game" if self.in_game else "free"

    def _fetch(self, tool: str, arguments: dict[str, Any]) -> Any:
        if self._call is not None:
            return self._call(tool, arguments)
        token = read_token(self.token_path, self.root)
        if token is None:
            raise OSError("no token")
        return mcp_call(self.url, token, tool, arguments, 5.0)

    async def poll(self, scope: Scope, current: Callable[[], None]) -> list[Event]:
        try:
            state = await asyncio.to_thread(self._fetch, "getGameState", {})
            chat = await asyncio.to_thread(self._fetch, "getRecentChat", {"n": CHAT_LINES})
        except OSError, ValueError, urllib.error.URLError:
            # The tunnel is off, the game is closed or the token changed: nothing to report.
            self.reachable = False
            self.in_game = False
            return []
        current()
        self.reachable = True
        if not isinstance(state, dict):
            self.in_game = False
            self._alive = False
            return self._death_watch([])
        await self._join_settings(state)
        mobs = await self._mob_rows(state)
        events = self._events(state, mobs)
        events += self._chat_events(chat if isinstance(chat, list) else [])
        events += self._death_watch(events)
        if self.in_game:
            events += await self._stuck_watch()
            events += await self._screen_watch()
        if self.reflex is not None and self.in_game:
            await self._reflexes(state, mobs, any(e.kind == "hurt" for e in events))
        return events

    def _death_watch(self, events: list[Event]) -> list[Event]:
        """She is out of the world for a minute after her health ran low: she cannot respawn by
        herself, so she is told to ask the operator. A normal respawn takes seconds and says
        nothing here."""
        now = self.clock()
        if self.in_game and self._alive:
            self._gone_since, self._dead_told = None, False
            return []
        if self._gone_since is None:
            self._gone_since = now
            return []
        wait = self.reflex.stance.dead_wait if self.reflex is not None else 60.0
        if self._dying and not self._dead_told and now - self._gone_since >= wait:
            self._dead_told = True
            text = (
                "You died and have not been back in the world for a minute: you cannot respawn "
                "on your own. Ask the operator with ticket_raise."
            )
            return [Event("game", "stuck_dead", "dead", text, Level.CRITICAL)]
        return []

    async def _reflexes(
        self, state: dict[str, Any], mobs: list[dict[str, Any]], hurt: bool
    ) -> None:
        assert self.reflex is not None
        me = state.get("self") if isinstance(state.get("self"), dict) else {}
        hostiles: list[Hostile] = []
        for row in mobs:
            if str(row.get("is_hostile")).lower() != "true":
                continue
            try:
                distance = float(row.get("distance", 1e9))
            except ValueError:
                continue
            hostiles.append(
                Hostile(str(row.get("type", "mob"))[:30], distance, _floats(row.get("position")))
            )
        players: list[tuple[str, float]] = []
        for row in state.get("players") if isinstance(state.get("players"), list) else []:
            try:
                players.append((str(row["name"])[:40], float(row["distance"])))
            except KeyError, TypeError, ValueError:
                continue
        now = self.clock()
        threat = ""
        if hurt or now - self.reflex.hurt_at <= self.reflex.stance.hurt_window:
            try:
                got = await asyncio.to_thread(self._fetch, "getThreatStatus", {})
                threat = got if isinstance(got, str) else ""
            except OSError, ValueError, urllib.error.URLError:
                threat = ""
        actions = self.reflex.decide(
            now,
            float(me.get("hp", 0) or 0),
            _floats(me.get("pos")),
            sorted(hostiles, key=lambda h: h.distance),
            hurt,
            sorted(players, key=lambda pair: pair[1]),
            threat,
        )
        await self._act(actions)

    async def _act(self, actions: list[Action]) -> None:
        for action in actions:
            calls: list[tuple[str, dict[str, Any]]]
            if action.kind == "stop":
                calls = [("stopPathing", {}), ("punkStop", {}), ("runAwayStop", {})]
            elif action.kind == "fight_mobs":
                calls = [("ExecuteCommand", {"cmd": "@hero"})]
            elif action.kind == "flee_to" and action.point is not None:
                x, y, z = action.point
                calls = [("gotoXYZ", {"x": x, "y": y, "z": z})]
            elif action.kind == "punk":
                calls = [("punk", {"name": action.target})]
            elif action.kind == "run_away":
                calls = [("runAwayPlayer", {"name": action.target, "distance": action.distance})]
            else:
                continue
            for tool, arguments in calls:
                try:
                    await asyncio.to_thread(self._fetch, tool, arguments)
                except (OSError, ValueError, urllib.error.URLError) as error:
                    LOG.warning("Reflex %s failed: %s", tool, type(error).__name__)

    async def close(self) -> None:
        """STOP or an epoch change: whatever the reflex started in the game is stopped."""
        if self.reflex is not None and self.reflex.mode != "idle":
            self.reflex.reset()
            await self._act([Action("stop")])

    async def _join_settings(self, state: dict[str, Any]) -> None:
        """Once per entry into a world: the pathfinder does not place or break blocks on its own."""
        if not state.get("inGame"):
            self._set_up = False
            return
        if self._set_up:
            return
        self._set_up = True
        for name in JOIN_SETTINGS:
            try:
                await asyncio.to_thread(
                    self._fetch, "ExecuteCommand", {"cmd": f"@set {name} false"}
                )
            except (OSError, ValueError, urllib.error.URLError) as error:
                LOG.warning("Join setting %s failed: %s", name, type(error).__name__)
                self._set_up = False
                return

    async def _screen_watch(self) -> list[Event]:
        """An open screen (inventory, chat, a menu) stops all walking. If one stays open for a
        while, remind her: she may be using a menu, so nothing is closed for her."""
        try:
            screen = await asyncio.to_thread(self._fetch, "getOpenScreen", {})
        except OSError, ValueError, urllib.error.URLError:
            return []
        now = self.clock()
        if not isinstance(screen, dict) or not screen.get("open"):
            self._screen_since = None
            return []
        if self._screen_since is None:
            self._screen_since = now
            return []
        if now - self._screen_since < SCREEN_WINDOW or now - self._screen_told < SCREEN_AGAIN:
            return []
        self._screen_told = now
        text = (
            f"A screen has been open for {SCREEN_WINDOW:.0f} seconds. While it is open, nothing "
            "walks and nothing is placed. If you are not using it, close it with the "
            "closeOpenScreen tool (the operator can also press Esc)."
        )
        return [Event("game", "screen_open", "screen", text, Level.HIGH)]

    async def _stuck_watch(self) -> list[Event]:
        """The mod keeps trying to walk and the body does not get anywhere (a wall, a height it
        cannot climb): stop the pointless pushing and tell her to choose another way."""
        try:
            status = await asyncio.to_thread(self._fetch, "pathStatus", {})
        except OSError, ValueError, urllib.error.URLError:
            return []
        if not isinstance(status, dict) or not status.get("busy"):
            self._walk.clear()
            return []
        pos = _floats(status.get("pos"))
        if pos is None:
            return []
        now = self.clock()
        self._walk = [(t, p) for t, p in self._walk if now - t <= STUCK_WINDOW] + [(now, pos)]
        if now - self._walk[0][0] < STUCK_WINDOW * 0.9:
            return []  # not long enough watched yet
        away = max(math.dist(pos, p) for _, p in self._walk)
        if away >= STUCK_MOVE or now - self._stuck_told < STUCK_AGAIN:
            return []
        self._stuck_told = now
        self._walk.clear()
        goal = status.get("goal", "?")
        text = (
            f"You have been trying to reach {goal} for {STUCK_WINDOW:.0f} seconds and the body "
            "is not moving: the way is blocked or too high. The platform stopped the pushing. "
            "Pick another direction or a lower or closer point; do not repeat the same route."
        )
        target = _floats(goal)
        me = self.state.get("self") if isinstance(self.state.get("self"), dict) else {}
        if target is not None and target[1] - pos[1] >= 3:
            blocks = me.get("blocks")
            have = blocks if blocks is not None else "some"
            text += (
                f" The goal is {target[1] - pos[1]:.0f} blocks higher than you and climbing "
                f"means placing blocks under your feet (you carry {have}): "
                "without blocks the jumping never ends. Look for a slope or stairs, or get blocks."
            )
        if self.reflex is not None:
            await self._act([Action("stop")])
        return [Event("game", "stuck", "walk", text, Level.HIGH)]

    async def _mob_rows(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        """Non-player entities near her, nearest first, when the mod offers the tool."""
        if not state.get("inGame"):
            return []
        if self._mob_pause > 0:
            self._mob_pause -= 1
            return []
        try:
            rows = await asyncio.to_thread(self._fetch, "getEntitiesInfo", {"limit": 10})
        except OSError, ValueError, urllib.error.URLError:
            return []
        if not isinstance(rows, list):
            self._mob_pause = MOB_PAUSE  # an older mod without the tool: do not ask every time
            return []
        return [row for row in rows if isinstance(row, dict)]

    @staticmethod
    def _hostiles(rows: list[dict[str, Any]]) -> list[tuple[str, float]]:
        found: list[tuple[str, float]] = []
        for row in rows:
            if str(row.get("is_hostile")).lower() != "true":
                continue
            try:
                found.append((str(row.get("type", "mob"))[:30], float(row.get("distance", 1e9))))
            except ValueError:
                continue
        return sorted(found, key=lambda pair: pair[1])

    # --- state --------------------------------------------------------------------------
    def _events(
        self, state: dict[str, Any], mobs: list[dict[str, Any]] | None = None
    ) -> list[Event]:
        events: list[Event] = []
        was_in = self.in_game
        self.in_game = bool(state.get("inGame"))
        if not self.in_game:
            self._set_up = False
            self._alive = False
            if was_in:
                events.append(
                    Event("game", "left", "left", "You are no longer in a world (menu or "
                          "disconnect).", Level.HIGH)
                )  # fmt: skip
            self._prev = None
            self._near.clear()
            self._mobs.clear()
            return events
        me = state.get("self") if isinstance(state.get("self"), dict) else {}
        self.state = state
        hp = float(me.get("hp", 0) or 0)
        top = float(me.get("maxHp", 20) or 20)
        self._alive = hp > 0
        if hp <= top * LOW_HP:
            self._dying = True
        elif hp >= top * 0.6:
            self._dying = False
        food = float(me.get("food", 20) or 20)
        where = f"hp {hp:.0f}/{top:.0f}, food {food:.0f}, at {me.get('pos', '?')}, " + str(
            me.get("dimension", "")
        )
        close = [(t, d) for t, d in self._hostiles(mobs or []) if d <= MOB_NEAR]
        if close:
            where += "; near: " + ", ".join(f"{t} {d:.0f}m" for t, d in close[:3])
        prev = self._prev
        if not was_in:
            events.append(Event("game", "joined", "joined", "You are in the world.", Level.NORMAL))
        if prev is not None:
            lost = float(prev["hp"]) - hp
            before = _floats(prev.get("pos"))
            after = _floats(me.get("pos"))
            moved = math.dist(before, after) if before and after else 0.0
            respawned = (
                float(prev["hp"]) <= top * LOW_HP
                and hp >= top * 0.9
                and (moved > 20 or prev.get("dimension") != me.get("dimension"))
            )
            if respawned:
                events.append(
                    Event("game", "died", "died", "You probably died and respawned.",
                          Level.CRITICAL, context=where)
                )  # fmt: skip
            elif lost >= 1:
                events.append(
                    Event(
                        "game", "hurt", "hp", f"You lost {lost:.0f} hp.", Level.HIGH, context=where
                    )
                )
        low = hp <= top * LOW_HP
        if low and not self._low_hp:
            events.append(
                Event("game", "low_health", "low", f"Health is low: {hp:.0f}/{top:.0f}.",
                      Level.CRITICAL, context=where)
            )  # fmt: skip
        if hp >= top * 0.6:
            self._low_hp = False
        elif low:
            self._low_hp = True
        hungry = food <= 6
        if hungry and not self._low_food:
            events.append(
                Event("game", "hungry", "food", f"Food is low: {food:.0f}/20.", Level.NORMAL)
            )
        if food >= 14:
            self._low_food = False
        elif hungry:
            self._low_food = True
        types = {t for t, _ in close}
        for kind in sorted(types - self._mobs):
            distance = min(d for t, d in close if t == kind)
            text = f"A hostile {kind} is {distance:.0f} blocks away."
            events.append(Event("game", "hostile_near", kind, text, Level.HIGH))
        self._mobs = types
        events += self._players(state.get("players"))
        self._prev = {
            "hp": hp,
            "pos": me.get("pos"),
            "dimension": me.get("dimension"),
        }
        return events

    def _players(self, rows: Any) -> list[Event]:
        """A player coming close is news, not an alarm: one line for all who came, and the same
        name is not reported again for ten minutes (a lobby has crowds)."""
        now = self.clock()
        near: set[str] = set()
        came: list[tuple[str, float]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name", ""))[:40]
            try:
                distance = float(row.get("distance", 1e9))
            except TypeError, ValueError:
                continue
            if not name or distance > NEAR:
                continue
            near.add(name)
            if name not in self._near and now - self._near_told.get(name, -1e9) >= NEAR_AGAIN:
                self._near_told[name] = now
                came.append((name, distance))
        self._near = near
        if not came:
            return []
        came.sort(key=lambda pair: pair[1])
        if len(came) == 1:
            name, distance = came[0]
            text = f"Player {name} is near: {distance:.0f} blocks."
        else:
            names = ", ".join(name for name, _ in came[:4])
            text = f"{len(came)} players came near: {names}."
        return [Event("game", "player_near", "players", text, Level.NORMAL)]

    # --- chat ---------------------------------------------------------------------------
    async def delivered(self, scope: Scope, current: Callable[[], None]) -> None:
        return None  # the game keeps nothing "unseen" of its own

    @staticmethod
    def _sender(line: str) -> str | None:
        """The nick before the arrow or colon (the first one that looks like a Minecraft name)."""
        found = SENDER.search(line)
        return (found.group(1) or found.group(2)) if found else None

    def _known(self, name: str) -> str:
        """What she has written down about this player (at most two short lines)."""
        if self.people is None or self.root is None:
            return ""
        try:
            path = self.people
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 200_000:
                return ""
            if not path.resolve().is_relative_to(self.root.resolve()):
                return ""
            rows = [r.strip() for r in path.read_text(encoding="utf-8").splitlines()]
        except OSError:
            return ""
        mine = [r for r in rows if name.casefold() in r.casefold()]
        return " | ".join(r[:160] for r in mine[:2])

    def _own(self, line: str) -> bool:
        """Her own name opens the line as the sender: <Name> text, Name: text, [rank] Name >>."""
        head = line[:40].casefold()
        return any(re.search(re.escape(name) + r"\s*[>:\u00bb\]]", head) for name in self.names)

    def _chat_events(self, lines: list[Any]) -> list[Event]:
        clean = [SECTION.sub("", str(line)).strip()[:240] for line in lines]
        clean = [line for line in clean if line]
        # New lines = what follows the longest tail that we already saw.
        fresh = clean
        for size in range(min(len(clean), len(self._seen_chat)), 0, -1):
            if clean[:size] == self._seen_chat[-size:]:
                fresh = clean[size:]
                break
        else:
            fresh = clean if self._seen_chat else []  # the first look: remember, do not report
        self._seen_chat = clean
        events: list[Event] = []
        talk, last = 0, ""
        now = self.clock()
        for line in fresh:
            if self._own(line):
                continue  # her own words in the chat are not news for her
            sender = self._sender(line)
            if self._mine(line, sender):
                continue  # the same, under the nick of her bot
            if self.book is not None and sender is not None:
                try:  # a proof code may be typed in the game chat by anyone
                    self.book.redeem("minecraft", sender, sender, line)
                except Exception:  # noqa: BLE001 - the book never stops the game watch
                    LOG.warning("People book failed on a game line")
            named = any(name in line.casefold() for name in self.names)
            following = sender is not None and self._talk.get(sender, 0.0) > now
            if named or following:
                if sender is not None:
                    self._talk[sender] = now + TALK_WINDOW  # the talk goes on while he answers
                known = self._known(sender) if sender else ""
                if self.book is not None and sender is not None:
                    try:
                        person = self.book.seen("minecraft", sender, sender)
                        seen = self.book.glimpse(person, "minecraft") if person else ""
                    except Exception:  # noqa: BLE001
                        seen = ""
                    known = " | ".join(part for part in (known, seen) if part)
                if self.on_talk is not None:
                    if sender is not None and sender.casefold() in BOTS:
                        continue  # the mod's and the server's own lines are no player's talk
                    try:
                        self.on_talk(line, known)
                    except Exception:  # noqa: BLE001 - the fast voice never stops the game watch
                        LOG.warning("Game talk hook failed")
                    continue
                events.append(
                    Event(
                        "game",
                        "talk",
                        sender or "chat",
                        line,
                        Level.HIGH,
                        context=("you know: " + known) if known else "",
                    )  # fmt: skip
                )
            else:
                talk += 1
                last = line
        if talk:
            events.append(
                Event(
                    "game",
                    "chat",
                    "chat",
                    f"{talk} new chat line(s), last: {last}",
                    Level.LOW,
                    count=talk,
                )
            )
        return events
