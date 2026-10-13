"""Relational identity, access and transaction ledger around validated contract revisions."""

from alembic import op
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()
scopes = Table(
    "scopes",
    metadata,
    Column("scope_id", String(64), primary_key=True),
    Column("workspace_id", String(128), nullable=False),
    Column("persona_id", String(128), nullable=False),
    Column("memory_space_id", String(128), nullable=False),
    Column("memory_mode", String(4), nullable=False),
    CheckConstraint("memory_mode IN ('work', 'test')"),
)
grants = Table(
    "storage_grants",
    metadata,
    Column("scope_id", String(64), primary_key=True),
    Column("principal_id", String(128), primary_key=True),
    Column("owner", String(32), primary_key=True),
    Column("role", String(16), nullable=False),
    Column("actor_owner", String(32)),
    Column("read", Boolean, nullable=False),
    Column("write", Boolean, nullable=False),
    Column("api_transfer", Boolean, nullable=False),
    Column("public_disclosure", Boolean, nullable=False),
    Column("expires_at", String(32)),
    ForeignKeyConstraint(["scope_id"], ["scopes.scope_id"]),
    CheckConstraint("role IN ('core_owner', 'operator', 'worker')"),
)
records = Table(
    "records",
    metadata,
    Column("scope_id", String(64), primary_key=True),
    Column("kind", String(64), primary_key=True),
    Column("id", String(128), primary_key=True),
    Column("owner", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("active_workspace_id", String(128)),
    Column("created_at", String(32), nullable=False),
    Column("updated_at", String(32), nullable=False),
    Column("expires_at", String(32)),
    Column("deleted", Boolean, nullable=False, default=False),
    Column("available", Boolean, nullable=False, default=True),
    Column("read_allowed", Boolean, nullable=False, default=True),
    Column("api_transfer_allowed", Boolean, nullable=False, default=True),
    Column("public_disclosure_allowed", Boolean, nullable=False, default=True),
    ForeignKeyConstraint(["scope_id"], ["scopes.scope_id"]),
    CheckConstraint("version >= 1"),
    UniqueConstraint("active_workspace_id", name="uq_one_active_workspace"),
)
identity = ["scope_id", "kind", "id"]
revisions = Table(
    "record_revisions",
    metadata,
    Column("scope_id", String(64), primary_key=True),
    Column("kind", String(64), primary_key=True),
    Column("id", String(128), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("schema_version", String(16), nullable=False),
    Column("payload", Text, nullable=False),
    Column("expires_at", String(32)),
    ForeignKeyConstraint(identity, ["records." + key for key in identity]),
    CheckConstraint("version >= 1 AND schema_version = '1.0'"),
    CheckConstraint("json_valid(payload) AND length(CAST(payload AS BLOB)) <= 1048576"),
    CheckConstraint("json_extract(payload, '$.id') = id"),
    CheckConstraint("json_extract(payload, '$.schema_version') = schema_version"),
)
keys = Table(
    "record_keys",
    metadata,
    Column("scope_id", String(64), primary_key=True),
    Column("namespace", String(64), primary_key=True),
    Column("key", String(64), primary_key=True),
    Column("kind", String(64), nullable=False),
    Column("id", String(128), nullable=False),
    ForeignKeyConstraint(identity, ["records." + key for key in identity]),
)
access = Table(
    "record_access",
    metadata,
    Column("scope_id", String(64), primary_key=True),
    Column("kind", String(64), primary_key=True),
    Column("id", String(128), primary_key=True),
    Column("principal_id", String(128), primary_key=True),
    Column("read", Boolean, nullable=False),
    Column("api_transfer", Boolean, nullable=False),
    Column("public_disclosure", Boolean, nullable=False),
    Column("expires_at", String(32)),
    ForeignKeyConstraint(identity, ["records." + key for key in identity]),
)
links = Table(
    "source_links",
    metadata,
    Column("scope_id", String(64), primary_key=True),
    Column("kind", String(64), primary_key=True),
    Column("id", String(128), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("source_id", String(128), primary_key=True),
    Column("source_version", Integer, primary_key=True),
    Column("availability", String(16), nullable=False),
    ForeignKeyConstraint(
        [*identity, "version"], ["record_revisions." + key for key in [*identity, "version"]]
    ),
)
outbox = Table(
    "outbox",
    metadata,
    Column("event_id", String(128), primary_key=True),
    Column("scope", Text, nullable=False),
    Column("operation_id", String(128), nullable=False),
    Column("causal_id", String(128)),
    Column("scope_id", String(64), nullable=False),
    Column("owner", String(32), nullable=False),
    Column("kind", String(64), nullable=False),
    Column("resource_id", String(128), nullable=False),
    Column("resource_version", Integer, nullable=False),
    Column("request_id", String(128), nullable=False),
    Column("created_at", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("delivery_count", Integer, nullable=False),
    Column("next_attempt_at", String(32)),
    ForeignKeyConstraint(["scope_id"], ["scopes.scope_id"]),
    CheckConstraint("status IN ('pending', 'delivered', 'failed') AND delivery_count >= 0"),
)
operations = Table(
    "processed_operations",
    metadata,
    Column("scope_id", String(64), primary_key=True),
    Column("request_id", String(128), primary_key=True),
    Column("owner", String(32), nullable=False),
    Column("principal_id", String(128), nullable=False),
    Column("command_type", String(64), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("receipt", Text),
    Column("receipt_id", String(128), nullable=False),
    Column("expires_at", String(32), nullable=False),
    Column("created_at", String(32), nullable=False),
    ForeignKeyConstraint(["scope_id"], ["scopes.scope_id"]),
    CheckConstraint(
        "receipt IS NULL OR (json_valid(receipt) AND length(CAST(receipt AS BLOB)) <= 1048576)"
    ),
)
Index("ix_records_page", records.c.scope_id, records.c.kind, records.c.id)
Index("ix_source_dependents", links.c.scope_id, links.c.source_id)
Index("ix_outbox_pending", outbox.c.status, outbox.c.next_attempt_at, outbox.c.created_at)

FOUNDATION = (scopes, grants)
CONTENT = (records, revisions, keys, access, links, outbox, operations)

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in CONTENT:
        table.create(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("DESTRUCTIVE_DOWNGRADE_UNSUPPORTED")
