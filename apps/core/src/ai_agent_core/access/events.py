"""Bounded canonical event fan-out. Producers remain trusted state owners."""

from collections import deque
from dataclasses import dataclass, field

from ai_agent_contracts.api import EventSubscription, PanelEvent, StreamChunk, StreamTerminal
from ai_agent_contracts.codec import ContractError, to_wire
from ai_agent_contracts.states import ErrorCode

from .sessions import AccessLimits


@dataclass(eq=False, repr=False)
class Subscriber:
    subscription: EventSubscription
    buffer: deque[bytes] = field(default_factory=deque, repr=False)
    closed: bool = False
    notifications_only: bool = False


class Events:
    def __init__(self, limits: AccessLimits) -> None:
        self.limits = limits
        self._clients: set[Subscriber] = set()

    def subscribe(
        self, subscription: EventSubscription, *, notifications_only: bool = False
    ) -> Subscriber:
        if subscription.after_event_id is not None:
            # No replay store yet: refresh projections; never restart a model call.
            raise ContractError(ErrorCode.STALE_CONTEXT)
        if len(self._clients) >= self.limits.websocket_clients:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        client = Subscriber(
            subscription.model_copy(deep=True), notifications_only=notifications_only
        )
        self._clients.add(client)
        return client

    def remove(self, client: Subscriber) -> None:
        self._clients.discard(client)
        client.closed = True
        client.buffer.clear()

    def publish(self, event: PanelEvent) -> None:
        self._publish(event)

    def private_stream(self, event: StreamChunk | StreamTerminal) -> None:
        # Trusted private owner only. It cannot be confused with a public delivery.
        if event.scope.agent_id is None or event.scope.run_id is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        self._publish(event)

    def _publish(self, event: PanelEvent | StreamChunk | StreamTerminal) -> None:
        raw = to_wire(event)
        for client in tuple(self._clients):
            if client.notifications_only and not isinstance(event, PanelEvent):
                continue
            selected = client.subscription
            if event.scope.partition != selected.scope.partition:
                continue
            if any(
                getattr(selected.scope, name) is not None
                and getattr(selected.scope, name) != getattr(event.scope, name)
                for name in ("agent_id", "run_id", "session_id")
            ):
                continue
            if (
                selected.resource_ids
                and getattr(event, "resource_id", getattr(event, "utterance_id", None))
                not in selected.resource_ids
            ):
                continue
            if len(client.buffer) >= self.limits.websocket_events:
                self.remove(client)
            else:
                client.buffer.append(raw)
