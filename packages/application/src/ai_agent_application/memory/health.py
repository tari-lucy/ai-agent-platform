"""Visible index lag: sources committed to the authority but not (yet) reflected in the index."""

from ai_agent_contracts.common import Scope


class IndexHealth:
    def __init__(self) -> None:
        self._pending: dict[tuple[str, str, str, str], set[str]] = {}

    def mark(self, scope: Scope, source_id: str) -> None:
        self._pending.setdefault(scope.partition, set()).add(source_id)

    def clear(self, scope: Scope, source_id: str | None = None) -> None:
        if source_id is None:
            self._pending.pop(scope.partition, None)
        else:
            self._pending.get(scope.partition, set()).discard(source_id)

    def pending(self, scope: Scope) -> frozenset[str]:
        return frozenset(self._pending.get(scope.partition, ()))
