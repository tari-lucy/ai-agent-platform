"""Story rules (hard / soft / desired) in layers, and the story screen that applies them.

Layers are project > profile > session. A lower layer can add rules but can neither reuse a higher
layer's rule id nor loosen it. Neither the game master, the character nor reflection can change one.
Matching has two stages: operator-provided markers (deterministic, always on) and an optional
classifier port (e.g. a model); an unavailable or unsure classifier is *uncertain*, which closes the
proposal instead of letting it through.
"""

import unicodedata
from dataclasses import dataclass
from typing import Literal, Protocol

type Level = Literal["project", "profile", "session"]
type Kind = Literal["hard", "soft", "desired"]
LEVEL_ORDER: tuple[Level, ...] = ("project", "profile", "session")


@dataclass(frozen=True)
class StoryRule:
    rule_id: str
    level: Level
    kind: Kind
    description: str
    markers: tuple[str, ...] = ()  # case-folded substrings that signal the forbidden theme


@dataclass(frozen=True)
class StoryRules:
    version: int
    rules: tuple[StoryRule, ...]

    def __post_init__(self) -> None:
        ids = [rule.rule_id for rule in self.rules]
        if len(set(ids)) != len(ids) or self.version < 1:
            raise ValueError("STORY_RULES_INVALID")

    @staticmethod
    def layered(
        version: int,
        project: tuple[StoryRule, ...] = (),
        profile: tuple[StoryRule, ...] = (),
        session: tuple[StoryRule, ...] = (),
    ) -> StoryRules:
        """Combine layers; a lower layer may not redefine or weaken a higher layer's rule."""
        merged: list[StoryRule] = []
        for level, rules in zip(LEVEL_ORDER, (project, profile, session), strict=True):
            for rule in rules:
                if rule.level != level:
                    raise ValueError("STORY_RULE_LEVEL")
                merged.append(rule)
        return StoryRules(version, tuple(merged))

    def forbidden(self) -> tuple[StoryRule, ...]:
        return tuple(r for r in self.rules if r.kind in ("hard", "soft"))


@dataclass(frozen=True)
class Classification:
    hard: tuple[str, ...] = ()
    soft: tuple[str, ...] = ()
    uncertain: bool = False


class StoryClassifier(Protocol):
    async def classify(self, text: str, rules: tuple[StoryRule, ...]) -> Classification: ...


@dataclass(frozen=True)
class ScreenResult:
    allowed: bool
    reason: str  # OK | HARD:<id> | SOFT:<id> | UNCERTAIN | SCREEN_UNAVAILABLE
    rule_ids: tuple[str, ...] = ()
    needs_operator: bool = False


def fold(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


async def screen(
    rules: StoryRules, text: str, classifier: StoryClassifier | None = None
) -> ScreenResult:
    folded = fold(text)
    hard: list[str] = []
    soft: list[str] = []
    for rule in rules.forbidden():
        if any(marker and fold(marker) in folded for marker in rule.markers):
            (hard if rule.kind == "hard" else soft).append(rule.rule_id)
    uncertain = False
    if classifier is not None:
        try:
            result = await classifier.classify(text, rules.forbidden())
        except Exception:
            return ScreenResult(False, "SCREEN_UNAVAILABLE", needs_operator=True)
        known = {r.rule_id: r for r in rules.forbidden()}
        for rule_id in (*result.hard, *result.soft):
            matched = known.get(rule_id)
            if matched is None:  # a rule that does not exist is an unusable answer
                return ScreenResult(False, "SCREEN_UNAVAILABLE", needs_operator=True)
            target = hard if matched.kind == "hard" else soft
            if rule_id not in target:
                target.append(rule_id)
        uncertain = result.uncertain
    if hard:
        return ScreenResult(False, f"HARD:{hard[0]}", tuple(hard))
    if soft:
        return ScreenResult(False, f"SOFT:{soft[0]}", tuple(soft), needs_operator=True)
    if uncertain:
        return ScreenResult(False, "UNCERTAIN", needs_operator=True)
    return ScreenResult(True, "OK")
