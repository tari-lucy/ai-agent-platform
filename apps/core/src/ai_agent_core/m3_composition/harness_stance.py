"""Reflexes in the game: what the platform does at once, without a model, when she is in danger.

The operator's rules (2026-11-02): a hostile mob near -> fight it while her health is above the
threshold, run when it is not; a player who hit her first -> hit back; death -> keep playing, and
ask the operator only when she cannot get back into the world. Everything else stays her own
decision. The reflex only ever *stops* what the mod is doing or starts one of its combat or
movement tools; it never touches money, files or the platform. STOP ends it: the watcher calls
``close`` and the game is told to stop.
"""

import math
from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["fight_mobs", "flee_to", "punk", "run_away", "stop"]


@dataclass(frozen=True)
class Stance:
    mob_fight_hp: float = 10.0  # fight hostile mobs above this, run at or below
    pvp: Literal["never", "retaliate"] = "retaliate"
    near: float = 8.0  # blocks: a hostile closer than this is dealt with
    calm: float = 16.0  # blocks: no hostile within this = the fight is over
    flee_distance: int = 25
    player_near: float = 10.0  # a player this close can be the one who hit her
    hurt_window: float = 12.0  # seconds a hit counts as "just now" for who-hit-me
    dead_wait: float = 60.0  # seconds out of the world after dying before the operator is asked
    repeat: float = 10.0  # least seconds before the same command is sent again


@dataclass(frozen=True)
class Action:
    kind: Kind
    target: str = ""
    point: tuple[int, int, int] | None = None
    distance: int = 0


@dataclass(frozen=True)
class Hostile:
    kind: str
    distance: float
    pos: tuple[float, float, float] | None


@dataclass
class Reflex:
    stance: Stance = field(default_factory=Stance)
    mode: Literal["idle", "fighting_mobs", "fleeing", "punking", "running_player"] = "idle"
    last_sent: dict[str, float] = field(default_factory=dict)
    calm_polls: int = 0
    hurt_at: float = -1e9

    def _due(self, key: str, now: float) -> bool:
        if now - self.last_sent.get(key, -1e9) < self.stance.repeat:
            return False
        self.last_sent[key] = now
        return True

    def decide(
        self,
        now: float,
        hp: float,
        me: tuple[float, float, float] | None,
        hostiles: list[Hostile],
        hurt: bool,
        players_near: list[tuple[str, float]],
        threat: str,
    ) -> list[Action]:
        """What to do on this look. ``players_near``: (name, distance) nearest first; ``threat``:
        the mod's text about who has been hurting her (empty when unknown)."""
        s = self.stance
        if hurt:
            self.hurt_at = now
        actions: list[Action] = []
        close = [h for h in hostiles if h.distance <= s.near]
        any_hostile = [h for h in hostiles if h.distance <= s.calm]

        # --- a player who hit her ------------------------------------------------------------
        attacker = ""
        if s.pvp == "retaliate" and now - self.hurt_at <= s.hurt_window and threat:
            low = threat.casefold()
            for name, distance in players_near:
                if distance <= s.player_near and name.casefold() in low:
                    attacker = name
                    break
        if attacker:
            if hp > s.mob_fight_hp:
                if self.mode != "punking" and self._due("punk", now):
                    actions += [Action("stop"), Action("punk", attacker)]
                    self.mode = "punking"
            elif self.mode != "running_player" and self._due("run", now):
                actions += [Action("stop"), Action("run_away", attacker, distance=s.flee_distance)]
                self.mode = "running_player"
            return actions

        # --- hostile mobs ---------------------------------------------------------------------
        if close:
            if hp > s.mob_fight_hp:
                if self.mode != "fighting_mobs" and self._due("hero", now):
                    actions += [Action("stop"), Action("fight_mobs")]
                    self.mode = "fighting_mobs"
            elif self.mode != "fleeing" and self._due("flee", now):
                point = self._away(me, close[0])
                if point is not None:
                    actions += [Action("stop"), Action("flee_to", point=point)]
                self.mode = "fleeing"
            self.calm_polls = 0
            return actions

        # --- nothing near: end what the reflex started ---------------------------------------
        if self.mode != "idle" and not any_hostile:
            self.calm_polls += 1
            if self.calm_polls >= 2 and self._due("calm", now):
                actions.append(Action("stop"))
                self.mode = "idle"
                self.calm_polls = 0
        return actions

    def _away(
        self, me: tuple[float, float, float] | None, hostile: Hostile
    ) -> tuple[int, int, int] | None:
        """A point ``flee_distance`` blocks from her, straight away from the hostile."""
        if me is None or hostile.pos is None:
            return None
        dx, dz = me[0] - hostile.pos[0], me[2] - hostile.pos[2]
        length = math.hypot(dx, dz) or 1.0
        far = self.stance.flee_distance
        return round(me[0] + dx / length * far), round(me[1]), round(me[2] + dz / length * far)

    def reset(self) -> None:
        self.mode, self.calm_polls = "idle", 0
