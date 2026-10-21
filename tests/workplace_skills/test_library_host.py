"""AW-03 on the production composition: offline enable, rights by operator decision, HTTP view.

Real serve build (ASGI), SQLite, deletion ledger and private blob directory; synthetic data.
The activity owner (AW-05) is stood in for by a directly registered attempt: what is checked
here is that the file owner lets an attempt touch an area only with a current Permission of the
workplace agent for that attempt's capability, granted through the operator's decision.
"""

import asyncio
import hashlib
import stat
from pathlib import Path

import pytest
from ai_agent_application.agent_knowledge import NoteDraft
from ai_agent_application.agent_skills import SkillDraft
from ai_agent_application.agent_workplace.ports import WorkAdmission
from ai_agent_application.memory import SourceDraft
from ai_agent_application.storage import Grant, Owner, RecordAccess
from ai_agent_application.tickets import TicketDraft
from ai_agent_contracts.agent_workplace import CapabilityRequest, FileWrite, WorkAttempt
from ai_agent_contracts.api import MessageArguments, MessageCommand, Permission, Receipt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.control import Freshness
from ai_agent_contracts.states import ErrorCode, Provenance
from quality_support import Model, prepared_host
from test_http import post, scope_params

from ai_agent_core.local_host import rights
from ai_agent_core.local_host.layout import HostError, load_config, paths
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.tickets import enable_tickets
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.state import PRINCIPALS, command

SKILLS = "workplace.skills"
BINDING = "workplace-library." + SKILLS
DRAFT = SkillDraft(
    skill_id="outline-a-text",
    title="Конспект текста",
    applicability="Нужно сжать длинный текст до тезисов.",
    limitations="Не для таблиц.",
    change_reason="Первая версия.",
)


async def attempt(rt, number: int, capability: str = SKILLS, inputs=()) -> WorkAdmission:
    """Stands in for the activity owner: the attempt is recorded before any IO."""
    run = rt.supervisor.snapshot.scope.model_copy(update={"agent_id": "agent", "run_id": "run"})
    fresh = Freshness(run_id="run", control_epoch=1, profile_version=1, policy_version=1)
    now = rt.supervisor.clock.utc()
    identifier = f"task.a{number:04d}"
    row = WorkAttempt(
        id=identifier,
        version=1,
        scope=run,
        created_at=now,
        updated_at=now,
        task_id="task",
        attempt_no=number,
        operation_id="operation-" + identifier,
        capability_id=capability,
        capability_version=1,
        freshness=fresh,
        state="prepared",
        inputs=list(inputs),
    )
    command = MessageCommand(
        request_id="attempt-" + identifier,
        scope=run,
        target="task",
        type="dialogue.message",
        arguments=MessageArguments(text=identifier),
    )
    async with rt.store.transaction(PRINCIPALS[Owner.ACTIVITY], run, command) as unit:
        await unit.put(row, access=(RecordAccess(PRINCIPALS[Owner.CATALOGUE].id),))
        await unit.complete(
            Receipt(
                request_id=command.request_id,
                scope=run,
                status="completed",
                resource_id=identifier,
                version=1,
            )
        )
    return WorkAdmission(run, fresh, row.operation_id, identifier, 1, lambda: True)


async def decide(host, request_id: str, decision: dict, version: int):
    preview = await post(
        host.env,
        dict(
            type="permission.decision.preview",
            request_id=request_id + "-preview",
            target=decision["capability_id"],
            scope=host.scope.model_dump(mode="json"),
            expected_version=version,
            arguments=dict(schema_id="permission.decision-args", data=decision),
        ),
    )
    assert preview.status_code == 200, preview.text
    done = await post(
        host.env,
        dict(
            type="permission.decision.execute",
            request_id=request_id,
            target=decision["capability_id"],
            scope=host.scope.model_dump(mode="json"),
            expected_version=1,
            arguments={"plan_id": preview.json()["resource_id"], "plan_version": 1},
        ),
    )
    assert done.status_code == 200, done.text


async def library(host):
    return await host.env.client.get("/api/v1/workplace-library", params=scope_params(host.scope))


def test_offline_enable_operator_grant_view_and_revoke(tmp_path, monkeypatch):
    async def scenario():
        host = await prepared_host(tmp_path, Model(), monkeypatch)
        with pytest.raises(HostError, match="RECOVERY_REQUIRED"):
            await enable_workplace_files(host.root)
        await enable_recovery(
            host.root,
            store_dir=tmp_path / "backups",
            ledger_path=tmp_path / "ledger" / "events.sqlite",
        )
        await enable_tickets(host.root)
        async with host.open():
            closed = await library(host)
            assert closed.status_code >= 400 and "CAPABILITY_UNAVAILABLE" in closed.text
            assert host.runtime.workplace is None
        settings = await enable_workplace_files(host.root, ceiling_days=7)
        assert stat.S_IMODE(Path(settings.blob_dir).stat().st_mode) & 0o077 == 0
        assert settings == await enable_workplace_files(host.root, ceiling_days=7)  # repeatable
        async with host.open():
            rt = host.runtime
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            # The activity owner's own rows arrive with AW-05; here they stand in for it.
            await rt.store.provision(
                host.scope,
                rights.host_grants(load_config(paths(host.root)))
                + (
                    Grant(PRINCIPALS[Owner.ACTIVITY], Owner.ACTIVITY, read=True, write=True),
                    Grant(PRINCIPALS[Owner.ACTIVITY], Owner.MEMORY, read=True),
                ),
            )
            place = rt.workplace
            assert place is not None
            # Enabled is not granted: without the agent's right the file owner writes nothing.
            with pytest.raises(ContractError) as denied:
                await place.skills.save(await attempt(rt, 1), "s-1", DRAFT, b"1. Read.\n", None)
            assert denied.value.error.code is ErrorCode.ACCESS_DENIED

            # The agent asks by ticket; the operator grants the skills area by decision.
            made = await rt.tickets.raise_ticket(
                "need-skills",
                host.scope,
                "agent",
                "agent",
                TicketDraft(
                    kind="capability_request",
                    title="Нужна библиотека навыков",
                    summary="Хочу сохранять свои методы.",
                    attempted="Пробовала записать метод.",
                    decision_needed="Разрешить библиотеку навыков.",
                    basis=SKILLS,
                    request=CapabilityRequest(
                        capability_id=SKILLS, areas=["skills"], constraints="Только навыки."
                    ),
                ),
                "Прошу доступ.",
            )
            ticket = await rt.tickets.get(host.scope, made.resource_id)
            grant = dict(
                action="grant",
                capability_id=SKILLS,
                binding_id=BINDING,
                ticket_id=ticket.id,
                ticket_version=ticket.version,
                level="automatic",
                duration_seconds=86400,
                areas=["skills"],
                constraints="Только навыки.",
            )
            await decide(host, "grant-1", grant, ticket.version)
            assert rt.supervisor.snapshot.state.value == "STOPPED"  # a grant starts nothing

            saved = await place.skills.save(
                await attempt(rt, 2), "s-2", DRAFT, b"1. Read.\n2. Outline.\n", None
            )
            assert (saved.version, saved.status) == (1, "experimental")
            _, method = await place.skills.read(await attempt(rt, 3), DRAFT.skill_id)
            assert method == b"1. Read.\n2. Outline.\n"
            # The skills right does not reach another area, nor does another capability's
            # attempt use it.
            with pytest.raises(ContractError):
                await place.knowledge.write(
                    await attempt(rt, 4),
                    "k-1",
                    NoteDraft("note-1", "notes/one.md", "Заметка", "note"),
                    "Текст.",
                    None,
                )
            with pytest.raises(ContractError):
                await place.skills.read(await attempt(rt, 5, "workplace.knowledge"), DRAFT.skill_id)

            view = await library(host)
            assert view.status_code == 200, view.text
            body = view.json()
            assert [s["skill_id"] for s in body["skills"]] == [DRAFT.skill_id]
            [method_file] = body["files"]
            assert (method_file["area"], method_file["kind"]) == ("skills", "method")
            assert body["complete"] is True

            # The operator opens a file's text in the panel, through the owner's guard.
            opened = await host.env.client.get(
                "/api/v1/workplace-file/" + saved.method.artifact_id,
                params=scope_params(host.scope),
            )
            assert opened.status_code == 200, opened.text
            assert opened.json()["text"] == "1. Read.\n2. Outline.\n"
            assert opened.json()["withheld"] is False
            unknown = await host.env.client.get(
                "/api/v1/workplace-file/no-such-file", params=scope_params(host.scope)
            )
            assert unknown.status_code >= 400
            # A file made from a source: once the source is blocked (ledger), no text is given.
            made = await memory(rt).register_source(
                command(host.scope, "src-basis"),
                SourceDraft(
                    provenance=Provenance.OPERATOR_INPUT,
                    source_type="operator-message",
                    adapter_id="dialogue",
                    adapter_version="1",
                    occurred_at=rt.supervisor.clock.utc(),
                    content="Основание заметки.",
                    read_allowed=True,
                    api_transfer_allowed=True,
                    public_disclosure_allowed=False,
                ),
            )
            basis = SourceRef(
                source_id=made.source_id,
                version=made.version,
                scope=host.scope,
                availability="available",
            )
            data = "Из основания.".encode()
            derived = await place.files.write(
                await attempt(rt, 7, inputs=[basis]),
                FileWrite(
                    request_id="derived-1",
                    file_id="derived-note",
                    expected_version=None,
                    area="skills",
                    path="outline-a-text/notes.md",
                    title="Заметка к навыку",
                    kind="note",
                    provenance="generated",
                    media_type="text/markdown",
                    checksum=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                ),
                data,
            )
            url = "/api/v1/workplace-file/" + derived.id
            shown = await host.env.client.get(url, params=scope_params(host.scope))
            assert shown.json()["text"] == "Из основания."
            await rt.maintenance.ledger.block(
                host.scope,
                basis.source_id,
                basis.version,
                "rights_revoked",
                rt.supervisor.clock.utc(),
            )
            hidden = await host.env.client.get(url, params=scope_params(host.scope))
            assert hidden.status_code == 200, hidden.text
            assert hidden.json()["withheld"] is True and hidden.json()["text"] is None

            # Revoked: the next read is refused at the file owner.
            [right] = [
                p
                for p in await rt.store.repository(
                    Permission, PRINCIPALS[Owner.WORKSPACE], host.scope
                ).list()
                if p.capability_id == SKILLS
            ]
            await decide(
                host,
                "revoke-1",
                dict(
                    action="revoke",
                    capability_id=SKILLS,
                    binding_id=BINDING,
                    permission_id=right.id,
                    permission_version=right.version,
                ),
                right.version,
            )
            with pytest.raises(ContractError) as revoked:
                await place.skills.read(await attempt(rt, 6), DRAFT.skill_id)
            assert revoked.value.error.code is ErrorCode.ACCESS_DENIED

    asyncio.run(scenario())
