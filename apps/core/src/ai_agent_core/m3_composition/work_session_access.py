"""The initial right for the configured work conversation capability (R5 launcher).

When a stored operator message waits after START and the session capability is configured
(manifest + ready binding) but has no usable right, the platform — not the character — opens one
technical capability request through the existing ticket owner. No model call, no Permission:
the operator decides through the structured permission decision. One ticket per capability
version and binding merges every repeat; a refusal is never reopened here, and a request is
raised again only after the operator resolved the old one. The right covers the capability
within its areas, term and the shared budget, not one message or conversation.
"""

from typing import TYPE_CHECKING, Literal

from ai_agent_application.agent_activity.book import area
from ai_agent_application.storage import Owner
from ai_agent_application.tickets import TicketDraft, duplicate_key, ticket_id
from ai_agent_contracts.agent_sessions import WorkSession
from ai_agent_contracts.agent_workplace import (
    CapabilityBinding,
    CapabilityManifest,
    CapabilityRequest,
    Ticket,
)
from ai_agent_contracts.common import Scope

from .state import PRINCIPALS

if TYPE_CHECKING:
    from .service import Runtime

type Waiting = Literal["unavailable", "access_required", "access_requested", "access_refused"]
TITLE = "Для рабочего разговора требуется разрешение"
AUTHOR = "work-sessions"


async def _configured(
    rt: Runtime, scope: Scope, capability: str
) -> tuple[CapabilityManifest, CapabilityBinding] | None:
    owner = PRINCIPALS[Owner.WORKSPACE]
    manifests = await rt.store.repository(CapabilityManifest, owner, scope).list(limit=200)
    bindings = await rt.store.repository(CapabilityBinding, owner, scope).list(limit=200)
    for manifest in manifests:
        if manifest.capability_id != capability:
            continue
        for binding in bindings:
            if (
                binding.capability_id == capability
                and binding.state == "ready"
                and binding.capability_version == manifest.capability_version
            ):
                return manifest, binding
    return None


def _basis(manifest: CapabilityManifest, binding: CapabilityBinding) -> str:
    return (
        f"work-session-access {manifest.capability_id} "
        f"v{manifest.capability_version} {binding.binding_id}"
    )


async def access(rt: Runtime, scope: Scope) -> tuple[Waiting | None, Ticket | None]:
    """Why work messages wait for the conversation capability; None while it is usable."""
    from .activity import tools

    capability = rt.session_capability
    if rt.session_harness is None or capability is None or rt.tickets is None:
        return "unavailable", None
    if any(
        tool.available and tool.manifest.capability_id == capability
        for tool in await tools(rt, scope, session=True)
    ):
        return None, None
    found = await _configured(rt, scope, capability)
    if found is None:
        return "unavailable", None
    ticket = await rt.tickets.get(
        scope, ticket_id(duplicate_key("capability_request", _basis(*found)))
    )
    if ticket is None or ticket.state == "resolved":
        return "access_required", ticket
    if ticket.state == "closed_unfulfilled":
        return "access_refused", ticket
    return "access_requested", ticket


async def _waiting(rt: Runtime, sessions: list[WorkSession]) -> bool:
    from .work_sessions import sessions as history

    for session in sessions:
        if session.state not in ("open", "waiting") or session.pending_reply is not None:
            continue
        messages = await history(rt).messages(
            session.scope, session.id, after_sequence=session.processed_sequence, limit=200
        )
        if any(message.speaker == "operator" for message in messages):
            return True
    return False


async def request(rt: Runtime, scope: Scope, sessions: list[WorkSession]) -> None:
    """Open (once) the technical access request while an operator message waits."""
    waiting, ticket = await access(rt, scope)
    if waiting != "access_required" or not await _waiting(rt, sessions):
        return
    assert rt.tickets is not None and rt.session_capability is not None
    found = await _configured(rt, scope, rt.session_capability)
    assert found is not None
    manifest, binding = found
    basis = _basis(manifest, binding)
    identifier = ticket_id(duplicate_key("capability_request", basis))
    receipt = await rt.tickets.raise_ticket(
        # Retried ticks replay this exact request; a new one only after the operator resolved.
        f"{AUTHOR}-{identifier}-{0 if ticket is None else ticket.version}",
        scope,
        "service",
        AUTHOR,
        TicketDraft(
            kind="capability_request",
            title=TITLE,
            summary=(
                "Технический запрос платформы, не решение персонажа: в рабочем разговоре ждёт "
                "сообщение оператора, а права на настроенный рабочий контур нет."
            ),
            attempted="Сообщение сохранено; модель не вызывалась, ничего не исполнялось.",
            decision_needed=(
                "Разрешить или отклонить использование рабочего контура в пределах его областей "
                "и срока; вызовы модели — в общем бюджете деятельности. Право действует для всех "
                "рабочих разговоров до истечения или отзыва."
            ),
            basis=basis,
            request=CapabilityRequest(
                capability_id=manifest.capability_id,
                areas=list(manifest.areas),
                constraints="Рабочий контур в пределах настроенных областей и общего бюджета.",
            ),
        ),
        TITLE + ".",
    )
    assert receipt.resource_id is not None
    rt.notify("ticket.changed", area(scope), receipt.resource_id, receipt.version)
