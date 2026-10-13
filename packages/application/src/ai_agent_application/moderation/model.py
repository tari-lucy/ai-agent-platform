"""Policy, the moderator's closed verdict schema and the gate's results (M2-07)."""

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Literal

from ai_agent_contracts.common import Contract, Id, ShortText
from pydantic import Field

MAX_RULES = 100
MAX_FRAGMENTS = 20

type Severity = Literal["block", "revise"]
type Outcome = Literal["approved", "revise", "blocked", "review", "unavailable"]


@dataclass(frozen=True)
class Rule:
    rule_id: Id
    title: str
    instruction: str
    severity: Severity = "block"


@dataclass(frozen=True)
class Policy:
    """One versioned rule set. It is data given by the operator; the model cannot change it."""

    policy_id: Id
    version: int
    rules: tuple[Rule, ...]
    source: str = "operator"
    max_revisions: int = 2

    def __post_init__(self) -> None:
        ids = [rule.rule_id for rule in self.rules]
        if not 1 <= len(ids) <= MAX_RULES or len(set(ids)) != len(ids) or self.version < 1:
            raise ValueError("POLICY_INVALID")

    @property
    def digest(self) -> str:
        material = [self.policy_id, self.version, self.max_revisions]
        material += [[r.rule_id, r.title, r.instruction, r.severity] for r in self.rules]
        return hashlib.sha256(json.dumps(material, ensure_ascii=False).encode()).hexdigest()

    def rule(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules if r.rule_id == rule_id), None)


class Fragment(Contract):
    start: Annotated[int, Field(strict=True, ge=0)]
    end: Annotated[int, Field(strict=True, ge=0)]


class Verdict(Contract):
    """What the moderator model may say. Anything else is an unusable answer."""

    decision: Literal["approve", "revise", "block", "review"]
    rule_ids: list[Id] = Field(default_factory=list, max_length=MAX_RULES)
    fragments: list[Fragment] = Field(default_factory=list, max_length=MAX_FRAGMENTS)
    reason: Annotated[ShortText, Field(max_length=300)] = ""


@dataclass(frozen=True)
class ModerationContext:
    """What the moderator sees besides the text (channels/audience come from the utterance)."""

    provenance: str = "model_proposal"
    previous: str | None = None  # the preceding line of the conversation, if relevant

    def canonical(self) -> dict[str, object]:
        previous = (
            None
            if self.previous is None
            else hashlib.sha256(self.previous.encode("utf-8")).hexdigest()
        )
        return {"provenance": self.provenance, "previous": previous}


@dataclass(frozen=True)
class CheckResult:
    outcome: Outcome
    check_id: str
    reason_code: str
    rule_ids: tuple[str, ...] = ()
    fragments: tuple[tuple[int, int], ...] = ()
    explanation: str = ""
    sent_to_model: bool = False


@dataclass(frozen=True)
class ReleaseResult:
    released: bool
    reason: str
