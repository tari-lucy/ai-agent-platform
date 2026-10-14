"""Retention periods (doc23 §5). Starting values; they are planning choices and configurable."""

from dataclasses import dataclass
from datetime import timedelta

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode

MAX_PERIOD = timedelta(days=3650)


@dataclass(frozen=True)
class RetentionPolicy:
    conversation: timedelta = timedelta(days=30)
    # Counted from the last change of the test source: no read tracking exists (see README).
    test_source: timedelta = timedelta(days=30)
    job: timedelta = timedelta(days=30)
    llm_metadata: timedelta = timedelta(days=90)
    aggregate: timedelta = timedelta(days=365)

    def __post_init__(self) -> None:
        periods = (self.conversation, self.test_source, self.job, self.llm_metadata, self.aggregate)
        if any(not timedelta(0) < period <= MAX_PERIOD for period in periods):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        # Aggregates replace per-attempt metadata: they must outlive it, or history vanishes.
        if self.aggregate < self.llm_metadata:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
