"""OC-01 bridge wire v1. These are transport views, never new domain records."""

import hashlib
import json
from typing import Literal

from ai_agent_contracts.api import Page, PanelEvent
from ai_agent_contracts.common import UTC, Contract, Id, Scope, ShortText, Version
from pydantic import Field

PREFIX = "/api/v1/opencode"
COMMANDS = frozenset(
    {
        "work.session.open",
        "work.session.message",
        "work.session.close",
        "work.session.rename",
        "agent.start",
        "dialogue.acknowledge",
    }
)


def directory(scope: Scope) -> str:
    """Stable virtual project identity, not a path, credential or selected runtime."""
    raw = json.dumps(scope.partition, ensure_ascii=True, separators=(",", ":")).encode()
    return "aw_" + hashlib.sha256(raw).hexdigest()


class AgentCard(Contract):
    directory: Id
    scope: Scope
    profile_id: Id
    profile_version: Version
    # Profiles currently have no distinct display-name field. Do not infer one from canon.
    label: ShortText
    active_runtime: bool


class Catalogue(Contract):
    protocol: Literal["aw-opencode/1"] = "aw-opencode/1"
    items: list[AgentCard] = Field(max_length=8)
    complete: Literal[True] = True


class SessionCard(Contract):
    created_at: UTC
    updated_at: UTC
    id: Id
    scope: Scope
    title: ShortText
    version: Version
    generation: Version
    state: Literal["open", "waiting", "closed", "invalidated"]
    last_sequence: int = Field(strict=True, ge=0)
    processed_sequence: int = Field(strict=True, ge=0)


class SessionPage(Page[SessionCard]):
    pass


class Invalidation(Contract):
    """Owner event -> refetch hint. No private text or fictitious delivery/ACK."""

    protocol: Literal["aw-opencode/1"] = "aw-opencode/1"
    directory: Id
    event_id: Id
    type: Literal["refetch"] = "refetch"
    owner_event: str
    resource_id: Id
    resource_version: Version | None


def invalidation(event: PanelEvent, selected: Scope) -> Invalidation | None:
    """Caller must use authenticated owner events, with their scope/lifecycle checks."""
    if event.scope.partition != selected.partition:
        return None
    return Invalidation(
        directory=directory(selected),
        event_id=event.event_id,
        owner_event=event.type,
        resource_id=event.resource_id,
        resource_version=event.resource_version,
    )
