"""Explicit administrator-only migration runner; startup never silently creates a schema."""

from pathlib import Path

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection

HEAD = "0002"


def migrate(connection: Connection, directory: Path, revision: str = "head") -> None:
    if not (directory / "env.py").is_file():
        raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
    config = Config()
    config.set_main_option("script_location", str(directory))
    config.attributes["connection"] = connection
    command.upgrade(config, revision)
