"""Read-only deletion impact over all workplace revisions, never a second ledger.

Text from an inseparable derived document cannot be selectively subtracted. Its
whole body/history must be invalidated, including material-linked skills/tickets.
The retention owner's operator preview must show this impact before activation.
"""

import hashlib
import json
from collections import defaultdict, deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from ai_agent_contracts.common import Scope
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_agent_adapters.storage import schema as db
from ai_agent_adapters.storage.policy import scope_key

KINDS = frozenset(
    {
        "ManagedFile",
        "SkillVersion",
        "Ticket",
        "TicketMessage",
        "WorkTask",
        "WorkAttempt",
        "WorkSession",
        "WorkSessionMessage",
        "Utterance",
    }
)
OWNERS = ("catalogue", "skills", "tickets", "activity")
type Identity = tuple[str, str]


class LifecycleError(RuntimeError):
    """Fixed code only; caller does not log private payloads or paths."""


def _objects(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


@dataclass
class _Node:
    sources: set[str] = field(default_factory=set)
    parents: set[Identity] = field(default_factory=set)
    blobs: set[str] = field(default_factory=set)
    revisions: list[tuple[int, str]] = field(default_factory=list)


@dataclass(frozen=True)
class PurgeImpact:
    """IDs and revision fingerprints only. No text, paths or credentials."""

    source_ids: tuple[str, ...]
    records: tuple[tuple[str, str, str], ...]  # kind, id, all-revision fingerprint
    keep_blobs: frozenset[str]
    digest: str

    @property
    def identities(self) -> tuple[Identity, ...]:
        return tuple((kind, identifier) for kind, identifier, _ in self.records)


async def impact(
    connection: AsyncConnection,
    scope: Scope,
    source_ids: Sequence[str],
    *,
    max_rows: int = 10000,
    max_bytes: int = 32 * 1024 * 1024,
) -> PurgeImpact:
    """Bounded actual revisions, including hidden, expired and historic dependencies.

    Run under a writer reservation when using the result for removal. All bodies
    live in existing record_revisions and scope_key is the stable partition.
    """
    if type(max_rows) is not int or max_rows < 1 or type(max_bytes) is not int or max_bytes < 1:
        raise LifecycleError("WORKPLACE_SCAN_INVALID")
    selected = tuple(sorted(set(source_ids)))
    if not selected or len(selected) > 500:
        raise LifecycleError("WORKPLACE_SOURCES_INVALID")
    query = (
        select(
            db.revisions.c.kind, db.revisions.c.id, db.revisions.c.version, db.revisions.c.payload
        )
        .join(
            db.records,
            and_(
                db.records.c.scope_id == db.revisions.c.scope_id,
                db.records.c.kind == db.revisions.c.kind,
                db.records.c.id == db.revisions.c.id,
            ),
        )
        .where(
            db.revisions.c.scope_id == scope_key(scope),
            or_(
                db.records.c.owner.in_(OWNERS),
                and_(db.records.c.owner == "gateway", db.revisions.c.kind == "Utterance"),
            ),
            db.revisions.c.kind != "Artifact",
        )
    )
    nodes: dict[Identity, _Node] = {}
    count = size = 0
    rows = await connection.stream(query.execution_options(yield_per=16))
    try:
        async for kind, identifier, version, payload in rows:
            if (
                not isinstance(kind, str)
                or not isinstance(identifier, str)
                or not isinstance(version, int)
                or not isinstance(payload, str)
            ):
                raise LifecycleError("WORKPLACE_METADATA_INVALID")
            count += 1
            size += len(payload.encode("utf-8"))
            if count > max_rows or size > max_bytes:
                raise LifecycleError("WORKPLACE_SCAN_LIMIT")
            if kind not in KINDS:
                raise LifecycleError("WORKPLACE_KIND_UNSUPPORTED")
            node = nodes.setdefault((kind, identifier), _Node())
            node.revisions.append((version, hashlib.sha256(payload.encode()).hexdigest()))
            body = json.loads(payload)
            for ref in _objects(body):
                if "source_id" in ref and "availability" in ref and "scope" in ref:
                    node.sources.add(ref["source_id"])
                if "artifact_id" in ref and "checksum" in ref and "scope" in ref:
                    node.parents.add(("ManagedFile", ref["artifact_id"]))
            if kind == "ManagedFile":
                node.blobs.add(body["reference"]["checksum"])
                if body.get("produced_by_attempt"):
                    node.parents.add(("WorkAttempt", body["produced_by_attempt"]))
            elif kind == "WorkAttempt":
                subject = (
                    ("WorkSession", body["session_id"])
                    if body.get("session_id")
                    else ("WorkTask", body["task_id"])
                )
                node.parents.add(subject)
                nodes.setdefault(subject, _Node()).parents.add((kind, identifier))
                if body.get("session_id") and body.get("result_id"):
                    nodes.setdefault(("Utterance", body["result_id"]), _Node()).parents.add(
                        (kind, identifier)
                    )
            elif kind == "WorkTask" and body.get("parent_task_id"):
                node.parents.add(("WorkTask", body["parent_task_id"]))
            elif kind == "Ticket":
                node.parents.update(("WorkTask", task) for task in body["task_ids"])
            elif kind == "TicketMessage":
                node.parents.add(("Ticket", body["ticket_id"]))
            elif kind == "WorkSession":
                node.parents.update(("WorkTask", task) for task in body["task_ids"])
            elif kind == "WorkSessionMessage":
                node.parents.add(("WorkSession", body["session_id"]))
                nodes.setdefault(("WorkSession", body["session_id"]), _Node()).parents.add(
                    (kind, identifier)
                )
    finally:
        await rows.close()
    gone = {identity for identity, node in nodes.items() if node.sources.intersection(selected)}
    children: dict[Identity, set[Identity]] = defaultdict(set)
    for identity, node in nodes.items():
        for parent in node.parents:
            children[parent].add(identity)
    pending = deque(gone)
    while pending:
        for child in children[pending.popleft()]:
            if child not in gone:
                gone.add(child)
                pending.append(child)
    entries = tuple(
        sorted(
            (
                kind,
                identifier,
                hashlib.sha256(
                    json.dumps(
                        sorted(nodes[(kind, identifier)].revisions), separators=(",", ":")
                    ).encode()
                ).hexdigest(),
            )
            for kind, identifier in gone
            if nodes[(kind, identifier)].revisions
        )
    )
    keep = frozenset(
        digest for identity, node in nodes.items() if identity not in gone for digest in node.blobs
    )
    token = hashlib.sha256(
        json.dumps([scope.partition, selected, entries], separators=(",", ":")).encode()
    ).hexdigest()
    return PurgeImpact(selected, entries, keep, token)
