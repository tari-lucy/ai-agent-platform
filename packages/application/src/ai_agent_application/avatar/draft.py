"""A first guess of the rig mapping profile from the names a model offers (doc37 A0).

It only suggests: the operator reads the draft, fixes it and saves it. Names are matched by
fragments (English and Russian) in hotkeys first, then expressions. Nothing is invented: an
emotion with no match is listed in the notes and left out of the draft.
"""

from typing import Any

from .model import Emotion, ModelInfo

HINTS: dict[Emotion, tuple[str, ...]] = {
    Emotion.HAPPY: ("smile", "happy", "joy", "laugh", "grin", "радост", "улыб", "счаст", "смех"),
    Emotion.SAD: ("sad", "cry", "tear", "sorrow", "грус", "плак", "слез"),
    Emotion.ANGRY: ("angry", "anger", "mad", "rage", "злой", "злост", "серд"),
    Emotion.SURPRISED: ("surprise", "shock", "wow", "startle", "удив", "шок"),
    Emotion.THINKING: ("think", "hmm", "ponder", "thought", "дум", "размыш"),
    Emotion.EMBARRASSED: ("blush", "shy", "embarrass", "flush", "смущ", "румян", "стесн"),
    Emotion.SLEEPY: ("sleep", "tired", "yawn", "drowsy", "сон", "устал", "зев"),
}
NEUTRAL = ("reset", "neutral", "default", "normal", "clear", "сброс", "нейтр", "обычн")


def _find(names: list[str], hints: tuple[str, ...]) -> str | None:
    for name in names:
        low = name.casefold()
        if any(hint in low for hint in hints):
            return name
    return None


def draft_profile(info: ModelInfo) -> tuple[dict[str, Any], list[str]]:
    """``(profile dict, notes)``; the dict passes ``profile_from_json`` and fits ``info``."""
    hotkeys, expressions = sorted(info.hotkeys), sorted(info.expressions)
    notes: list[str] = []
    emotions: dict[str, Any] = {}
    for emotion, hints in HINTS.items():
        if (hotkey := _find(hotkeys, hints)) is not None:
            emotions[emotion.value] = {"hotkey": hotkey}
        elif (expression := _find(expressions, hints)) is not None:
            emotions[emotion.value] = {"expression": expression}
        else:
            notes.append(f"{emotion.value}: подходящего хоткея или выражения не нашлось")
    profile: dict[str, Any] = {"version": 1, "model": info.name or None, "emotions": emotions}
    if (reset := _find(hotkeys, NEUTRAL)) is not None:
        profile["neutral"] = {"hotkey": reset}
    else:
        notes.append("neutral: хоткея сброса не нашлось (лицо будет возвращаться без него)")
    mouth = next(
        (n for n in sorted(info.parameters) if "mouth" in n.casefold() and "open" in n.casefold()),
        None,
    ) or next((n for n in sorted(info.parameters) if "mouth" in n.casefold()), None)
    if mouth is not None:
        profile["mouth_parameter"] = mouth
    else:
        notes.append("рот: параметра с «mouth» в названии не нашлось, рот двигаться не будет")
    eyes = {
        n: 1.0
        for n in sorted(info.parameters)
        if n.casefold() in {"eyeopenleft", "eyeopenright", "eyeleftopen", "eyerightopen"}
    }
    if eyes:
        profile["rest"] = eyes  # without face tracking the eyes would stay shut
        notes.append("глаза: " + ", ".join(eyes) + " держатся открытыми (rest)")
    if not profile["model"]:
        del profile["model"]
    return profile, notes
