"""Scheduling metadata owned by jobs; accepted contract records stay authoritative."""

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
)

metadata = MetaData()
Table("scopes", metadata, Column("scope_id", String(64), primary_key=True))

schedules = Table(
    "job_schedules",
    metadata,
    Column("scope_id", String(64), primary_key=True),
    Column("operation_id", String(128), primary_key=True),
    Column("job_id", String(128), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("ready_at", String(32), nullable=False),
    Column("retry_safe", Boolean, nullable=False),
    ForeignKeyConstraint(["scope_id"], ["scopes.scope_id"]),
    CheckConstraint("attempts >= 0 AND attempts <= 2"),
)
attempts = Table(
    "job_attempts",
    metadata,
    Column("scope_id", String(64), primary_key=True),
    Column("operation_id", String(128), primary_key=True),
    Column("attempt_id", String(128), primary_key=True),
    Column("lease_version", Integer, nullable=False),
    Column("worker_id", String(128), nullable=False),
    Column("expires_at", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("finished_at", String(32)),
    Column("error_code", String(64)),
    ForeignKeyConstraint(
        ["scope_id", "operation_id"],
        ["job_schedules.scope_id", "job_schedules.operation_id"],
    ),
    CheckConstraint("lease_version >= 1 AND lease_version <= 2"),
    CheckConstraint("status IN ('running','succeeded','failed','cancelled','expired','unknown')"),
)
Index("ix_jobs_ready", schedules.c.ready_at, schedules.c.operation_id)

reconciliations = Table(
    "job_reconciliations",
    metadata,
    Column("scope_id", String(64), primary_key=True),
    Column("receipt_id", String(128), primary_key=True),
    Column("operation_id", String(128), nullable=False),
    Column("attempt_id", String(128), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("metadata_json", Text, nullable=False),
    ForeignKeyConstraint(
        ["scope_id", "operation_id", "attempt_id"],
        ["job_attempts.scope_id", "job_attempts.operation_id", "job_attempts.attempt_id"],
    ),
    CheckConstraint("json_valid(metadata_json) AND length(CAST(metadata_json AS BLOB)) <= 262144"),
)

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in (schedules, attempts, reconciliations):
        table.create(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("DESTRUCTIVE_DOWNGRADE_UNSUPPORTED")
