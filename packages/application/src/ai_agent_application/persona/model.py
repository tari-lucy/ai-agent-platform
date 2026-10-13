"""Validated profile content independent of storage, plus safe issue reporting."""

import hashlib
import json
from dataclasses import dataclass

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode

from .schemas import DevelopmentBounds, PropertyDefinitions, ValueMap


@dataclass(frozen=True)
class Issue:
    """Operator-facing finding. Messages never echo imported text (it may hold a secret)."""

    code: str
    message: str
    line: int | None = None
    section: str | None = None


@dataclass(frozen=True)
class ParsedProfile:
    canon: str
    style: str
    definitions: PropertyDefinitions
    initial_values: ValueMap
    bounds: DevelopmentBounds

    @property
    def digest(self) -> str:
        """Identity of the content, independent of Markdown formatting (list order counts)."""
        canonical = json.dumps(
            {
                "canon": self.canon,
                "style": self.style,
                "definitions": self.definitions.model_dump(mode="json"),
                "initial_values": self.initial_values.model_dump(mode="json"),
                "bounds": self.bounds.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def revision_id(self) -> str:
        return "rev-" + self.digest


@dataclass(frozen=True)
class ProfileDiff:
    canon_changed: bool
    style_changed: bool
    properties_added: tuple[str, ...]
    properties_removed: tuple[str, ...]
    properties_changed: tuple[str, ...]
    initial_values_changed: tuple[str, ...]
    bounds_changed: tuple[str, ...]


class ProfileImportRejected(ContractError):
    """INVALID_ARGUMENTS carrying operator-readable issues; nothing was written."""

    def __init__(self, issues: tuple[Issue, ...], request_id: str | None = None) -> None:
        super().__init__(ErrorCode.INVALID_ARGUMENTS, request_id)
        self.issues = issues
