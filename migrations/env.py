"""Migrations require a caller-supplied local connection; no env or credential loading."""

from alembic import context

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("EXPLICIT_MIGRATION_CONNECTION_REQUIRED")
context.configure(connection=connection, transactional_ddl=True)
with context.begin_transaction():
    context.run_migrations()
