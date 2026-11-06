"""Avatar vocabulary (doc37, D89): what the persona feels, never how a given rig shows it.

The core knows a small set of semantic emotions. A mapping profile (data, one per rig) turns
each of them into the model's own hotkeys, expressions and parameter values. Parameter names
of a concrete model never appear outside a profile.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

MAX_PROFILE_BYTES = 64 * 1024
MAX_NAME = 120


class Emotion(StrEnum):
    NEUTRAL = "neutral"
    HAPPY = "happy"
    SAD = "sad"
    ANGRY = "angry"
    SURPRISED = "surprised"
    THINKING = "thinking"
    EMBARRASSED = "embarrassed"
    SLEEPY = "sleepy"


class AvatarError(Exception):
    """Fixed codes only: no remote text, token, URL or model data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ParameterRange:
    minimum: float
    maximum: float


@dataclass(frozen=True)
class ModelInfo:
    """What the rig offers right now, discovered at connect (doc04)."""

    name: str
    hotkeys: Mapping[str, str] = field(default_factory=dict)  # hotkey name -> id
    expressions: frozenset[str] = frozenset()  # expression file names
    parameters: Mapping[str, ParameterRange] = field(default_factory=dict)


@dataclass(frozen=True)
class Look:
    """One emotion on one rig: any of a hotkey, an expression file, parameter values."""

    hotkey: str | None = None
    expression: str | None = None
    parameters: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Profile:
    """Rig-specific mapping. ``neutral`` is what the avatar returns to on silence and STOP."""

    emotions: Mapping[Emotion, Look]
    neutral: Look = field(default_factory=Look)
    mouth_parameter: str | None = None
    mouth_gain: float = 1.0
    model: str | None = None  # when set, a different loaded model fails the check
    # Parameters held all the time while the avatar is on: without face tracking a rig rests
    # with its eyes shut, because only tracking opens them (e.g. EyeOpenLeft/Right at 1).
    rest: Mapping[str, float] = field(default_factory=dict)

    def validate(self, info: ModelInfo) -> list[str]:
        """Problems of this profile against the loaded rig; empty means it fits.

        Names only: a missing hotkey is reported by its profile name, which is data the
        operator wrote, so it is safe to show.
        """
        problems: list[str] = []
        if self.model is not None and self.model != info.name:
            problems.append(f"model:{self.model}")
        looks = [("neutral", self.neutral), *((e.value, look) for e, look in self.emotions.items())]
        for label, look in looks:
            if look.hotkey is not None and look.hotkey not in info.hotkeys:
                problems.append(f"{label}:hotkey:{look.hotkey}")
            if look.expression is not None and look.expression not in info.expressions:
                problems.append(f"{label}:expression:{look.expression}")
            for name in look.parameters:
                if name not in info.parameters:
                    problems.append(f"{label}:parameter:{name}")
        if self.mouth_parameter is not None and self.mouth_parameter not in info.parameters:
            problems.append(f"mouth:parameter:{self.mouth_parameter}")
        problems.extend(f"rest:parameter:{n}" for n in self.rest if n not in info.parameters)
        return problems


def _look(raw: Any, label: str) -> Look:
    if not isinstance(raw, dict):
        raise AvatarError(f"profile:{label}")
    extra = set(raw) - {"hotkey", "expression", "parameters"}
    hotkey, expression, parameters = raw.get("hotkey"), raw.get("expression"), raw.get("parameters")
    if extra or any(v is not None and not _name(v) for v in (hotkey, expression)):
        raise AvatarError(f"profile:{label}")
    values: dict[str, float] = {}
    if parameters is not None:
        if not isinstance(parameters, dict) or len(parameters) > 32:
            raise AvatarError(f"profile:{label}")
        for key, value in parameters.items():
            if not _name(key) or isinstance(value, bool) or not isinstance(value, int | float):
                raise AvatarError(f"profile:{label}")
            values[key] = float(value)
    return Look(hotkey, expression, values)


def _name(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_NAME


def profile_from_json(raw: Any) -> Profile:
    """Parse a profile dict; any unknown key or wrong type is a fixed ``profile:...`` error."""
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise AvatarError("profile:version")
    allowed = {"version", "model", "emotions", "neutral", "mouth_parameter", "mouth_gain", "rest"}
    if set(raw) - allowed:
        raise AvatarError("profile:keys")
    emotions: dict[Emotion, Look] = {}
    entries = raw.get("emotions", {})
    if not isinstance(entries, dict):
        raise AvatarError("profile:emotions")
    for key, value in entries.items():
        try:
            emotion = Emotion(key)
        except ValueError:
            raise AvatarError(f"profile:emotion:{str(key)[:20]}") from None
        if emotion is not Emotion.NEUTRAL:
            emotions[emotion] = _look(value, key)
    mouth, gain, model = raw.get("mouth_parameter"), raw.get("mouth_gain", 1.0), raw.get("model")
    if mouth is not None and not _name(mouth):
        raise AvatarError("profile:mouth")
    if isinstance(gain, bool) or not isinstance(gain, int | float) or not 0 < gain <= 4:
        raise AvatarError("profile:mouth_gain")
    if model is not None and not _name(model):
        raise AvatarError("profile:model")
    rest = _look({"parameters": raw.get("rest", {})}, "rest").parameters
    return Profile(
        emotions, _look(raw.get("neutral", {}), "neutral"), mouth, float(gain), model, rest
    )


def load_profile(path: Path) -> Profile:
    """Read a profile file: a plain small file, never a link."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_PROFILE_BYTES:
            raise AvatarError("profile:file")
        return profile_from_json(json.loads(path.read_text(encoding="utf-8")))
    except OSError, ValueError:
        raise AvatarError("profile:file") from None


class AvatarPort(Protocol):
    """The transport to a rig. One implementation per program (VTube Studio first)."""

    async def connect(self) -> ModelInfo: ...

    async def hotkey(self, hotkey_id: str) -> None: ...

    async def expression(self, file: str, *, active: bool) -> None: ...

    async def inject(self, values: Mapping[str, float]) -> None: ...

    async def close(self) -> None: ...
