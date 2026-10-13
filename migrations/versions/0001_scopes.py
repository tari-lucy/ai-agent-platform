"""Relational identity, access and transaction ledger around validated contract revisions."""

from alembic import op
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    MetaData,
    String,
    Table,
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

FOUNDATION = (scopes, grants)

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in FOUNDATION:
        table.create(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("DESTRUCTIVE_DOWNGRADE_UNSUPPORTED")
