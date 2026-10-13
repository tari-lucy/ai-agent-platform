"""SQL-side visibility is applied before pagination, including source dependencies."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from ai_agent_application.storage import Owner, Principal, ReadPurpose
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import ExtensionPayload, Scope, SourceRef
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.states import ErrorCode
from pydantic import BaseModel
from sqlalchemy import and_, exists, func, literal, or_, select
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import FromClause

from . import schema as db


def stamp(value: datetime) -> str:
    offset = value.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def scope_key(scope: Scope) -> str:
    return hashlib.sha256(json.dumps(scope.partition, separators=(",", ":")).encode()).hexdigest()


def check_principal(principal: Principal) -> None:
    if principal.role == "worker":
        raise ContractError(ErrorCode.ACCESS_DENIED)
    if principal.role not in {"core_owner", "operator"}:
        raise ContractError(ErrorCode.ACCESS_DENIED)
    if principal.role == "core_owner" and not isinstance(principal.owner, Owner):
        raise ContractError(ErrorCode.ACCESS_DENIED)


def unexpired(column: Any, now: str) -> ColumnElement[bool]:
    return or_(column.is_(None), column > now)


def grant_identity(table: FromClause, principal: Principal) -> ColumnElement[bool]:
    return and_(
        table.c.principal_id == principal.id,
        table.c.role == principal.role,
        table.c.actor_owner.is_(None)
        if principal.owner is None
        else table.c.actor_owner == principal.owner.value,
    )


def base_visible(
    row: FromClause,
    principal: Principal,
    purpose: ReadPurpose,
    now: str,
    *,
    allow_expired: bool = False,
) -> ColumnElement[bool]:
    if purpose not in {"private", "api_transfer", "public_disclosure"}:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    grant = db.grants.alias()
    acl = db.access.alias()
    field = "read" if purpose == "private" else purpose
    granted = exists(
        select(1).where(
            grant.c.scope_id == row.c.scope_id,
            grant.c.owner == row.c.owner,
            grant_identity(grant, principal),
            grant.c.read.is_(True),
            grant.c[field].is_(True),
            unexpired(grant.c.expires_at, now),
        )
    )
    own = literal(principal.role == "core_owner") & (
        row.c.owner == (principal.owner.value if principal.owner else "")
    )
    shared = exists(
        select(1).where(
            acl.c.scope_id == row.c.scope_id,
            acl.c.kind == row.c.kind,
            acl.c.id == row.c.id,
            acl.c.principal_id == principal.id,
            acl.c.read.is_(True),
            acl.c[field].is_(True),
            unexpired(acl.c.expires_at, now),
        )
    )
    return and_(
        row.c.deleted.is_(False),
        literal(True) if allow_expired else row.c.available.is_(True),
        literal(True) if allow_expired else unexpired(row.c.expires_at, now),
        row.c.read_allowed.is_(True),
        row.c[field + "_allowed"].is_(True),
        granted,
        or_(own, shared),
    )


def visible(
    row: FromClause,
    version: Any,
    principal: Principal,
    purpose: ReadPurpose,
    now: str,
) -> ColumnElement[bool]:
    link = db.links.alias()
    source = db.records.alias()
    revision = db.revisions.alias()
    valid_source: ColumnElement[bool] = exists(
        select(1)
        .select_from(
            source.join(
                revision,
                and_(
                    source.c.scope_id == revision.c.scope_id,
                    source.c.kind == revision.c.kind,
                    source.c.id == revision.c.id,
                ),
            )
        )
        .where(
            source.c.scope_id == link.c.scope_id,
            source.c.kind == "Source",
            source.c.id == link.c.source_id,
            revision.c.version == link.c.source_version,
            base_visible(source, principal, purpose, now),
            unexpired(revision.c.expires_at, now),
            func.json_extract(revision.c.payload, "$.availability") == "available",
        )
        .correlate(link)
    )
    # Exact obligations/identity survive raw-source TTL for private state reads.
    # Current grants and per-source ACL still apply; deleted content is never allowed.
    expired_source = exists(
        select(1)
        .select_from(
            source.join(
                revision,
                and_(
                    source.c.scope_id == revision.c.scope_id,
                    source.c.kind == revision.c.kind,
                    source.c.id == revision.c.id,
                ),
            )
        )
        .where(
            source.c.scope_id == link.c.scope_id,
            source.c.kind == "Source",
            source.c.id == link.c.source_id,
            revision.c.version == link.c.source_version,
            base_visible(source, principal, purpose, now, allow_expired=True),
            or_(
                source.c.expires_at <= now,
                revision.c.expires_at <= now,
                func.json_extract(revision.c.payload, "$.availability") == "expired",
            ),
        )
        .correlate(link)
    )
    exact_private = and_(
        literal(purpose == "private"),
        row.c.kind.in_(["Person", "Topic", "Promise", "Relationship"]),
    )
    valid_source = or_(valid_source, and_(exact_private, expired_source))
    invalid_link = exists(
        select(1)
        .where(
            link.c.scope_id == row.c.scope_id,
            link.c.kind == row.c.kind,
            link.c.id == row.c.id,
            link.c.version == version,
            or_(
                and_(
                    link.c.availability != "available",
                    ~and_(exact_private, link.c.availability == "expired"),
                ),
                ~valid_source,
            ),
        )
        .correlate(row, db.revisions)
    )
    return and_(base_visible(row, principal, purpose, now), ~invalid_link)


def source_refs(value: object, extensions: ExtensionRegistry | None = None) -> list[SourceRef]:
    found: list[SourceRef] = []
    if isinstance(value, SourceRef):
        found.append(value)
    elif isinstance(value, ExtensionPayload):
        if extensions is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        parsed = extensions.validate(value.schema_id, value.schema_version, value.data)
        found.extend(source_refs(parsed, extensions))
    elif isinstance(value, BaseModel):
        for key in type(value).model_fields:
            found.extend(source_refs(getattr(value, key), extensions))
    elif isinstance(value, (tuple, list)):
        for item in value:
            found.extend(source_refs(item, extensions))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(source_refs(item, extensions))
    return found
