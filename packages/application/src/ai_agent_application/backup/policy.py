"""Backup policy (doc03/doc23): 7 daily copies, a separate copy before migration, a quota."""

from dataclasses import dataclass
from datetime import timedelta

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode


@dataclass(frozen=True)
class BackupPolicy:
    keep_daily: int = 7
    keep_pre_migration: int = 2
    # A copy is a second home for raw dialogue: it never outlives the 30-day text term.
    max_age: timedelta = timedelta(days=30)
    quota_bytes: int | None = None  # warnings at 80% / 90%, no new copy at 100%

    def __post_init__(self) -> None:
        if not 1 <= self.keep_daily <= 365 or not 1 <= self.keep_pre_migration <= 50:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if not timedelta(days=1) <= self.max_age <= timedelta(days=3650):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if self.quota_bytes is not None and self.quota_bytes < 1:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
