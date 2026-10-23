"""Owner calls for one admitted session; no SQL writes, alternate memory, or host paths."""

import json
from typing import TYPE_CHECKING, Any, cast

from ai_agent_application.agent_knowledge import NoteDraft
from ai_agent_application.agent_workplace.ports import WorkAdmission, WorkspaceRequest
from ai_agent_application.memory import derived_id
from ai_agent_application.tickets import TicketDraft
from ai_agent_contracts.agent_sessions import SessionTicketRef
from ai_agent_contracts.agent_workplace import CapabilityRequest, TicketKind
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode

from .activity import book, tools
from .work_session_runtime import Release
from .work_session_skills import OPERATIONS as SKILL_OPERATIONS
from .work_session_skills import call as skill_call
from .workplace import granted_areas
from .workplace_experience import ticket_release

if TYPE_CHECKING:
    from .service import Execution


class Workspace:
    def __init__(self, execution: Execution, admission: WorkAdmission) -> None:
        self.ex, self.gate = execution, admission

    async def call(self, admission: WorkAdmission, request: WorkspaceRequest) -> str:
        ex, rt = self.ex, self.ex.runtime
        if admission != self.gate or rt.workplace is None or rt.tickets is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        await ex.verify()
        owner = book(rt)
        attempt = await owner.attempts_repo(admission.scope).get(admission.attempt_id)
        if (
            attempt is None
            or attempt.operation_id != admission.operation_id
            or attempt.state != "submitted"
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        guard = Release(rt)
        await guard.verify(admission, attempt.inputs, attempt.input_files, "api_transfer")
        a = request.arguments
        allowed = {
            "capabilities": set(),
            "list_files": {"area"},
            "read_file": {"file_id"},
            "write_note": {"file_id", "path", "title", "kind", "body", "expected_version"},
            "raise_ticket": {"title", "body", "capability_id", "kind"},
            "read_ticket": {"ticket_id"},
            **SKILL_OPERATIONS,
        }
        if request.operation not in allowed or set(a) - allowed[request.operation]:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if request.operation in ("raise_ticket", "read_ticket"):
            from .work_sessions import sessions

            session = (
                None
                if attempt.session_id is None
                else await sessions(rt).get(admission.scope, attempt.session_id)
            )
            if session is None or session.state in ("closed", "invalidated"):
                raise ContractError(ErrorCode.ACCESS_DENIED)
            linked_ids = set(session.waiting_ticket_ids) | {
                ref.ticket_id for ref in attempt.session_tickets
            }
            if len(linked_ids) >= 20 and (
                request.operation == "raise_ticket" or a.get("ticket_id") not in linked_ids
            ):
                raise ContractError(ErrorCode.RESOURCE_BUSY)
        sources, files, written, linked = [], [], [], []
        result: Any
        key = derived_id("work-tool", admission.scope, admission.attempt_id, request.call_id)
        place = rt.workplace
        if request.operation == "capabilities":
            result = [
                {
                    "id": t.manifest.capability_id,
                    "available": t.available,
                    "summary": t.manifest.summary[:200],
                    "areas": t.manifest.areas,
                }
                for t in (await tools(rt, admission.scope, session=True))[:20]
            ]
        elif request.operation == "list_files":
            area = a.get("area", "")
            if area not in await granted_areas(rt, admission.scope, attempt.capability_id):
                raise ContractError(ErrorCode.ACCESS_DENIED)
            entries = await place.files.list(admission.scope, area, limit=10)
            for entry in entries:
                await guard.verify(admission, (), (entry.reference,), "api_transfer")
            files = [entry.reference for entry in entries]
            result = [
                {"id": e.id, "title": e.title, "path": e.path, "version": e.version}
                for e in entries
            ]
        elif request.operation == "read_file":
            entry, data = await place.files.read(admission, a.get("file_id", ""))
            await guard.verify(admission, (), (entry.reference,), "api_transfer")
            if not entry.reference.media_type.startswith(("text/", "application/json")):
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            files = [entry.reference]
            sources = list(entry.dependencies)
            text = data.decode("utf-8", errors="replace")
            result = {
                "id": entry.id,
                "version": entry.version,
                "text": text[:4000],
                "truncated": len(text) > 4000,
            }
        elif request.operation == "write_note":
            try:
                version = int(a["expected_version"]) if "expected_version" in a else None
                kind = a.get("kind", "note")
                if kind not in ("note", "hypothesis"):
                    raise ValueError()
                draft = NoteDraft(a["file_id"], a["path"], a["title"], kind)  # type: ignore[arg-type]
                entry = await place.knowledge.write(admission, key, draft, a["body"], version)
            except ValueError, KeyError:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
            files = written = [entry.reference]
            result = {"id": entry.id, "version": entry.version, "provenance": entry.provenance}
        elif request.operation in SKILL_OPERATIONS:
            result, sources, files, written = await skill_call(
                rt, guard, admission, attempt, request.operation, a, key
            )
        elif request.operation == "raise_ticket":
            title, body = a.get("title", ""), a.get("body", "")
            if not title.strip() or not body.strip():
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            capability = a.get("capability_id")
            kind = a.get("kind", "question")
            if kind not in ("question", "problem", "idea"):
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            try:
                request_capability = (
                    None
                    if capability is None
                    else CapabilityRequest(
                        capability_id=capability, areas=[], constraints=body[:2000]
                    )
                )
            except ValueError:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
            if len(attempt.inputs) > 50 or len(attempt.input_files) > 50:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            receipt = await rt.tickets.raise_ticket(
                key,
                admission.scope,
                "agent",
                "workplace-agent",
                TicketDraft(
                    kind=cast(TicketKind, kind) if capability is None else "capability_request",
                    title=title[:200],
                    summary=body,
                    attempted="Рабочая сессия.",
                    decision_needed=body,
                    basis=capability or title,
                    sources=tuple(attempt.inputs),
                    materials=tuple(attempt.input_files),
                    request=request_capability,
                ),
                body,
            )
            result = {"ticket_id": receipt.resource_id, "version": receipt.version}
            if receipt.resource_id is None or receipt.version is None:
                raise ContractError(ErrorCode.STALE_CONTEXT)
            linked = [SessionTicketRef(ticket_id=receipt.resource_id, version=receipt.version)]
        else:
            ticket = await rt.tickets.get(admission.scope, a.get("ticket_id", ""))
            release = ticket_release(rt)
            if ticket is None or not await release.open(admission.scope, ticket, "api_transfer"):
                raise ContractError(ErrorCode.ACCESS_DENIED)
            messages = await release.messages(
                admission.scope,
                ticket,
                await rt.tickets.messages(admission.scope, ticket.id),
                "api_transfer",
            )
            sources, files = list(ticket.sources), list(ticket.materials)
            linked = [SessionTicketRef(ticket_id=ticket.id, version=ticket.version)]
            result = {
                "id": ticket.id,
                "state": ticket.state,
                "title": ticket.title,
                "messages": [{"author": m.author, "body": m.body[:1000]} for m in messages[-5:]],
            }
        await ex.verify()
        await guard.verify(admission, sources, files, "api_transfer")
        # Metadata must be owner-recorded before the model can see the returned text.
        attempt = await owner.extend_inputs(attempt, sources, files, written, linked)
        ex.extra_sources = tuple(attempt.inputs)
        await guard.verify(admission, attempt.inputs, attempt.input_files, "api_transfer")
        await ex.verify()
        rt.notify("work.changed", admission.scope, attempt.id, attempt.version)
        return json.dumps(result, ensure_ascii=False)
