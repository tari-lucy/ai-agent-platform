"""Platform MCP for the harness (doc33 stage 2, D81): the platform's side of the boundary.

The agent's OpenCode reaches this through the agent gateway, which relays each JSON-RPC
message over its stdin/stdout channel: no network path from the container to the host.
Every call is checked here, at the effect boundary: the run must be ACTIVE in the epoch the
harness started with; memory is released only through its owners' rights at call time.
Texts returned are data, not rights.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from ai_agent_application.tickets import TicketDraft
from ai_agent_contracts.agent_workplace import CapabilityRequest, TicketKind
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode, RunState

if TYPE_CHECKING:
    from .harness_wakes import HarnessWakes
    from .service import Runtime

LOG = logging.getLogger(__name__)
PROTOCOL = "2025-06-18"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "persona_brief",
        "description": "Кто я сейчас: характер, стиль, состояние, открытые обязательства и что "
        "я помню по теме. Вызывай в начале новой задачи или разговора.",
        "inputSchema": {
            "type": "object",
            "properties": {"topic": {"type": "string", "description": "тема разговора"}},
        },
    },
    {
        "name": "memory_recall",
        "description": "Вспомнить из своей памяти (разговоры, факты, выжимки) по запросу.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "ticket_raise",
        "description": "Обращение к оператору: вопрос, проблема, идея или просьба о новом "
        "доступе (подключение, лимит, устройство). Текст обращения прав не выдаёт.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "kind": {"type": "string", "enum": ["question", "problem", "idea"]},
                "capability_id": {
                    "type": "string",
                    "description": "только для просьбы о доступе: идентификатор возможности",
                },
            },
            "required": ["title", "body"],
        },
    },
    {
        "name": "wake_me",
        "description": "Поставить себе будильник: через minutes минут платформа сама напишет "
        "тебе text в разговор, даже если оператора нет. Для долгих сборок, ожидания и "
        "продолжения работы позже. Работает только в запущенном агенте (после START).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "minimum": 1, "maximum": 10080},
                "text": {"type": "string", "description": "что напомнить себе"},
            },
            "required": ["minutes", "text"],
        },
    },
    {
        "name": "tickets",
        "description": "Мои обращения, по которым оператор ответил, или одно обращение по id.",
        "inputSchema": {
            "type": "object",
            "properties": {"ticket_id": {"type": "string"}},
        },
    },
]


class PlatformMcp:
    def __init__(
        self,
        runtime: Runtime,
        epoch: Callable[[], int | None],
        wakes: HarnessWakes | None = None,
    ) -> None:
        self.rt, self.epoch, self.wakes = runtime, epoch, wakes

    def _current(self) -> None:
        run = self.rt.supervisor.snapshot
        if run.state != RunState.ACTIVE or run.control_epoch != self.epoch():
            raise ContractError(ErrorCode.STALE_CONTEXT)

    async def handle(self, body: Any) -> Any:
        """One JSON-RPC message or a batch; None for notifications only."""
        if isinstance(body, list):
            replies = [reply for item in body if (reply := await self._one(item)) is not None]
            return replies or None
        return await self._one(body)

    async def _one(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            return None
        method, identifier = message["method"], message.get("id")
        if identifier is None:
            return None  # a notification (initialized, cancelled): nothing to answer
        raw = message.get("params")
        params: dict[str, Any] = raw if isinstance(raw, dict) else {}
        try:
            if method == "initialize":
                result: Any = {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "platform", "version": "1"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = await self._call(str(params.get("name", "")), params.get("arguments"))
            else:
                return _error(identifier, -32601, "method not found")
        except ContractError as error:
            result = _text(f"Отказ: {error.error.code.value}", error=True)
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    async def _call(self, name: str, arguments: Any) -> dict[str, Any]:
        a = {k: str(v) for k, v in arguments.items()} if isinstance(arguments, dict) else {}
        self._current()
        scope = self.rt.supervisor.snapshot.scope
        if name in ("persona_brief", "memory_recall"):
            from .workplace_experience import persona

            topic = a.get("topic" if name == "persona_brief" else "query", "").strip()
            if name == "memory_recall" and not topic:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            brief = await persona(self.rt).build(scope, topic[:1000])
            self._current()  # a STOP during the read: the text is not released
            text = brief.text
            if name == "memory_recall":
                marker = "Что я помню по теме:"
                text = text[text.index(marker) :] if marker in text else "Ничего не нашлось."
            return _text(text or "Сведений о личности нет.")
        if name == "ticket_raise":
            return await self._raise(scope, a)
        if name == "tickets":
            return await self._tickets(scope, a.get("ticket_id", "").strip())
        if name == "wake_me":
            if self.wakes is None:
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            minutes = a.get("minutes", "").strip()
            if not minutes.isdigit():
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            wake = await self.wakes.set(int(minutes), a.get("text", ""))
            return _text(f"Будильник {wake.id} на {wake.due_at.isoformat(timespec='minutes')} UTC.")
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)

    async def _raise(self, scope: Any, a: dict[str, str]) -> dict[str, Any]:
        tickets = self.rt.tickets
        if tickets is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        title, body = a.get("title", "").strip(), a.get("body", "").strip()
        kind, capability = a.get("kind", "question"), a.get("capability_id") or None
        if not title or not body or kind not in ("question", "problem", "idea"):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        try:
            request = (
                None
                if capability is None
                else CapabilityRequest(capability_id=capability, areas=[], constraints=body[:2000])
            )
        except ValueError:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
        key = "harness-" + hashlib.sha256(f"{title}\0{body}".encode()).hexdigest()[:32]
        receipt = await tickets.raise_ticket(
            key,
            scope,
            "agent",
            "workplace-agent",
            TicketDraft(
                kind=cast(TicketKind, kind) if capability is None else "capability_request",
                title=title[:200],
                summary=body,
                attempted="Работа в своём рабочем месте (OpenCode).",
                decision_needed=body,
                basis=capability or title,
                sources=(),
                materials=(),
                request=request,
            ),
            body,
        )
        return _text(json.dumps({"ticket_id": receipt.resource_id}, ensure_ascii=False))

    async def _tickets(self, scope: Any, identifier: str) -> dict[str, Any]:
        from .workplace_experience import ticket_release

        tickets = self.rt.tickets
        if tickets is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        release = ticket_release(self.rt)
        if not identifier:
            rows = []
            for pending in await tickets.pending_for_agent(scope):
                if await release.open(scope, pending, "api_transfer"):
                    rows.append({"id": pending.id, "state": pending.state, "title": pending.title})
            return _text(json.dumps(rows, ensure_ascii=False) if rows else "Новых ответов нет.")
        ticket = await tickets.get(scope, identifier)
        if ticket is None or not await release.open(scope, ticket, "api_transfer"):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        messages = await release.messages(
            scope, ticket, await tickets.messages(scope, ticket.id), "api_transfer"
        )
        self._current()
        return _text(
            json.dumps(
                {
                    "id": ticket.id,
                    "state": ticket.state,
                    "title": ticket.title,
                    "messages": [
                        {"author": m.author, "body": m.body[:2000]} for m in messages[-10:]
                    ],
                },
                ensure_ascii=False,
            )
        )


def _text(text: str, *, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _error(identifier: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}
