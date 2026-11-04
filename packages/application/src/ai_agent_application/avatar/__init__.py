"""Avatar (doc37): semantic emotion, rig mapping profile, mixer service. No transport here."""

from .model import (
    AvatarError,
    AvatarPort,
    Emotion,
    Look,
    ModelInfo,
    ParameterRange,
    Profile,
    load_profile,
    profile_from_json,
)
from .service import Avatar, Outcome

__all__ = [
    "Avatar",
    "AvatarError",
    "AvatarPort",
    "Emotion",
    "Look",
    "ModelInfo",
    "Outcome",
    "ParameterRange",
    "Profile",
    "load_profile",
    "profile_from_json",
]
