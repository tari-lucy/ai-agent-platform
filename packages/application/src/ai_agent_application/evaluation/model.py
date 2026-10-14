"""Scenario model for the quality evaluation: data steps, a trace of what happened, checks.

A scenario is *data*: what the visitors write, when sessions change, which world events and
platform faults occur. The model's replies are never scripted, so the same scenario runs against
the dry-run stand-in and, once authorised, a real model. Checks only look at platform guarantees
(identity, canon, rights, STOP); how good a reply is stays with the operator.
"""

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from ai_agent_application.laboratory import Finding, Lab, Visitor

OPERATOR = "operator"


@dataclass(frozen=True)
class Say:
    who: str  # a key of ``QualityScenario.people`` or ``OPERATOR``
    text: str
    directed: bool = True


@dataclass(frozen=True)
class Gap:
    """Virtual time passes (no wall-clock wait)."""

    days: float = 0.0
    hours: float = 0.0


@dataclass(frozen=True)
class NewSession:
    pass


@dataclass(frozen=True)
class Live:
    """A first-hand (simulated) experience of the character, stored as an episode."""

    text: str


@dataclass(frozen=True)
class Fact:
    """A world source. ``private`` withholds it from any model call (api_transfer is off)."""

    text: str
    key: str = ""
    private: bool = False
    author: str | None = None


@dataclass(frozen=True)
class Forget:
    """Delete the source registered by ``Fact(key=...)``."""

    key: str


@dataclass(frozen=True)
class Stop:
    pass


@dataclass(frozen=True)
class Restart:
    pass


@dataclass(frozen=True)
class Resume:
    pass


@dataclass(frozen=True)
class ProviderFault:
    """The next ``calls`` provider calls fail with ``failure`` (wrapper-level, no network)."""

    failure: str = "TRANSIENT"
    calls: int = 2


type Step = Say | Gap | NewSession | Live | Fact | Forget | Stop | Restart | Resume | ProviderFault


@dataclass
class Turn:
    index: int  # position among the scenario's steps
    session: int
    who: str
    text: str
    status: str | None
    reason: str | None
    reply: str | None
    dropped: tuple[str, ...]
    input_tokens: int | None
    output_tokens: int | None
    usage_sources: tuple[str, ...]
    latency_ms: float
    call_ids: tuple[int, ...] = ()  # indexes into ``Trace.calls`` made while answering

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self)) | {
            "dropped": list(self.dropped),
            "usage_sources": list(self.usage_sources),
            "call_ids": list(self.call_ids),
        }


@dataclass
class Call:
    """One provider call seen by the recording wrapper. The prompt text stays in memory only."""

    role: str
    model: str
    prompt: str = field(repr=False)
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_source: str = "unknown"
    failure: str | None = None
    latency_ms: float = 0.0
    started: bool = False

    @property
    def prompt_sha(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "model": self.model,
            "prompt_chars": len(self.prompt),
            "prompt_sha": self.prompt_sha,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "usage_source": self.usage_source,
            "failure": self.failure,
            "latency_ms": round(self.latency_ms, 3),
        }


@dataclass
class Trace:
    turns: list[Turn] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    baseline: dict[str, int] = field(default_factory=dict)

    def prompts_of(self, index: int) -> list[str]:
        """Prompts of the provider calls made while answering the turn at this step index."""
        turn = self.turn(index)
        return [] if turn is None else [self.calls[i].prompt for i in turn.call_ids]

    def turn(self, index: int) -> Turn | None:
        return next((t for t in self.turns if t.index == index), None)


type Check = Callable[[Lab, Trace], Awaitable[list[Finding]]]
type Origin = Literal["doc22", "catalogue", "new"]


@dataclass(frozen=True)
class QualityScenario:
    scenario_id: str  # Q-01 .. Q-20
    slug: str
    title: str
    origin: Origin
    people: Mapping[str, Visitor]
    steps: tuple[Step, ...]
    expect: str  # what the operator should see (doc22 "ожидаемое наблюдение")
    failure: str  # what counts as a failure (doc22 "что считается неуспехом")
    focus: tuple[str, ...]  # rubric criteria that apply; empty = platform invariants only
    checks: tuple[Check, ...] = ()

    @property
    def sessions(self) -> int:
        return 1 + sum(isinstance(s, NewSession | Restart) for s in self.steps)

    @property
    def messages(self) -> int:
        return sum(isinstance(s, Say) for s in self.steps)
