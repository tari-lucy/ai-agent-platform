"""Normalised chat of a streaming platform (doc26 F4, doc37 A3): messages and removals.

What people write is untrusted audience data: the adapters only clean it (control characters,
length) and never interpret it. Platform IDs are kept so a deleted message or a banned author
can be forgotten again (removed text does not come back from a cache).
"""

import re
from dataclasses import dataclass

MAX_TEXT = 500
MAX_NAME = 40
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩]")


class ChatError(Exception):
    """Fixed code only: ``<platform>:<what>``; never a token, URL or remote text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def clean(text: str, limit: int = MAX_TEXT) -> str:
    return " ".join(_CONTROL.sub("", text).split())[:limit]


@dataclass(frozen=True)
class ChatMessage:
    platform: str
    id: str
    author_id: str
    author: str
    text: str
    at: float  # seconds since the epoch, as the platform said (or when it arrived)
    privileged: bool = False  # moderator, owner or the broadcaster


@dataclass(frozen=True)
class ChatRemoved:
    """A message was deleted (``id``), or all of an author's messages were (``author_id``)."""

    platform: str
    id: str = ""
    author_id: str = ""


ChatEvent = ChatMessage | ChatRemoved
