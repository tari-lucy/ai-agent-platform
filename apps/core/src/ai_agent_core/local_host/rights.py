"""The explicit rights matrix of a local host. `init` provisions it; `serve` never changes it.

Each row names the owner read/write that needs it. MODERATION and public disclosure are absent:
no public output exists in this host, and nothing is granted "to everyone".

The set is the smallest one that the covered flow passed with (see tests/local_host): login,
private dialogue with acknowledgment, TEST laboratory and report. Rows for PERSONA/GM
reading MEMORY, LLM reading WORKSPACE and CATALOGUE reading MEMORY (work scope) were removed after
the same flow still passed without them; JOBS->BEHAVIOR and BEHAVIOR/JOBS/GATEWAY->MEMORY were
each shown necessary by a failing run. SOCIAL (operator-authorised 2026-10-19 with its own
acceptance, tests/memory_quality/test_private_social_continuity.py) writes the private
interlocutor's Person/Encounter/Promise, reads MEMORY sources as evidence and CHANNEL deliveries
for promises; it never gets API transfer. The operator reads people/promises in the panel and
closes a promise only through the social owner (`promise.close`). Paths this host does not run yet
(extract, consolidate, gm and other jobs, which the mock leaves unanswered) get their reads only
with their own acceptance: until then they are denied, not widened.

Operator tickets (AW-01) are off by default: the offline `enable-tickets` adds only the
TICKETS owner rows (own records, MEMORY read for evidence) and the operator's TICKETS read.
A ticket never grants a right; no agent/runner principal reaches the permission owner.

Managed files, skills and knowledge (AW-03) are off by default: the offline
`enable-workplace-files` adds the SKILLS owner and the catalogue's reads of MEMORY (release
guard on sources) and ACTIVITY (the attempt that does the IO); SKILLS reads MEMORY only to check
evidence. Which area an agent may touch is never a grant here: it is a current Permission of
the workplace agent, checked by the file owner before and after IO.

Explicit --operator-controls is a separately accepted matrix: WORKSPACE plan/receipt
writes, operator WORKSPACE transfer reads and PERSONA/GM evidence reads. A separate
metadata-only TEST scope has no execution grants. Serve never provisions either mode.
"""

from typing import TYPE_CHECKING

from ai_agent_application.laboratory import lab_grants
from ai_agent_application.storage import Grant, Owner, RecordAccess

from ai_agent_core.m3_composition.state import OPERATOR, PRINCIPALS

if TYPE_CHECKING:
    from .layout import HostConfig

# Owners whose own records the m3 runtime, dialogue and lab paths write.
WRITERS = (
    Owner.WORKSPACE,  # Workspace/Connection/Capability/Permission
    Owner.PERSONA,  # Profile, CharacterState
    Owner.SUPERVISOR,  # run journal
    Owner.MEMORY,  # Source/Episode
    Owner.BEHAVIOR,  # ExecutionCommand
    Owner.JOBS,  # Job/Lease/ExecutionResult
    Owner.GATEWAY,  # Utterance, LLM gateway records
    Owner.CHANNEL,  # private panel Delivery
    Owner.GM,  # GmRuntimeState
    Owner.LLM,  # assignments, attempts, usage
    Owner.CATALOGUE,  # report Artifact
    Owner.LABORATORY,  # report, scope reservation
    Owner.SOCIAL,  # private interlocutor Person/Encounter, confirmed Promise
)
# Only these owners' records may be handed on over the loopback JSON transfer.
TRANSFER_OWNERS = (Owner.MEMORY, Owner.PERSONA, Owner.GM)
# Operator reads over HTTP (private dialogue, jobs, usage, people/promises), with explicit
# api_transfer for the panel.
OPERATOR_OWNERS = (
    Owner.SOCIAL,
    Owner.PERSONA,
    Owner.MEMORY,
    Owner.GATEWAY,
    Owner.CHANNEL,
    Owner.JOBS,
    Owner.LLM,
    Owner.GM,
)
# Operator reads in a TEST partition: reports and their catalogue/source records.
OPERATOR_LAB_OWNERS = (Owner.MEMORY, Owner.CATALOGUE, Owner.LABORATORY)


def core_grants(
    *,
    preparing: bool = False,
    operator_controls: bool = False,
    tickets: bool = False,
    activity: bool = False,
    files: bool = False,
) -> tuple[Grant, ...]:
    """Default WORKSPACE is read-only; explicit controls mode also stores plans/receipts.

    Existing grants are owner-level (not per-kind). The trusted WORKSPACE owner
    can therefore write its own records, including Permission. No HTTP permission
    editing is enabled; the operator never gets direct storage write access.
    """
    grants = [
        Grant(
            PRINCIPALS[o],
            o,
            read=True,
            write=preparing or operator_controls or o is not Owner.WORKSPACE,
            api_transfer=o in TRANSFER_OWNERS,
        )
        for o in WRITERS
    ]
    # Cross-owner reads, each with the reader that needs it.
    grants += [
        Grant(PRINCIPALS[Owner.JOBS], Owner.BEHAVIOR, read=True),
        Grant(PRINCIPALS[Owner.GATEWAY], Owner.MEMORY, read=True),
        Grant(PRINCIPALS[Owner.BEHAVIOR], Owner.MEMORY, read=True),
        Grant(PRINCIPALS[Owner.JOBS], Owner.MEMORY, read=True),
        Grant(PRINCIPALS[Owner.SOCIAL], Owner.MEMORY, read=True),  # evidence of a person/promise
        Grant(PRINCIPALS[Owner.SOCIAL], Owner.CHANNEL, read=True),  # a promise needs DELIVERED
    ]
    if operator_controls:
        grants += [Grant(PRINCIPALS[o], Owner.MEMORY, read=True) for o in (Owner.PERSONA, Owner.GM)]
    if tickets:
        # The tickets owner writes only its own records; it reads a source to check evidence.
        grants += [
            Grant(PRINCIPALS[Owner.TICKETS], Owner.TICKETS, read=True, write=True),
            Grant(PRINCIPALS[Owner.TICKETS], Owner.MEMORY, read=True),
        ]
    if activity:
        # The activity owner writes only tasks/attempts; executions read its tasks as jobs.
        # It reads MEMORY only to record which sources a step was shown (attempt inputs).
        grants += [
            Grant(PRINCIPALS[Owner.ACTIVITY], Owner.ACTIVITY, read=True, write=True),
            Grant(PRINCIPALS[Owner.ACTIVITY], Owner.MEMORY, read=True),
        ]
    if files:
        grants += [
            Grant(PRINCIPALS[Owner.CATALOGUE], Owner.MEMORY, read=True),
            Grant(PRINCIPALS[Owner.CATALOGUE], Owner.ACTIVITY, read=True),
            Grant(PRINCIPALS[Owner.SKILLS], Owner.SKILLS, read=True, write=True),
            Grant(PRINCIPALS[Owner.SKILLS], Owner.MEMORY, read=True),
        ]
    return tuple(grants)


def operator_grants(
    *, operator_controls: bool = False, tickets: bool = False, activity: bool = False
) -> tuple[Grant, ...]:
    return tuple(
        Grant(OPERATOR, o, read=True, api_transfer=True)
        for o in (
            *OPERATOR_OWNERS,
            *((Owner.WORKSPACE,) if operator_controls else ()),
            *((Owner.TICKETS,) if tickets else ()),
            *((Owner.ACTIVITY,) if activity else ()),
        )
    )


def work_grants(
    *,
    preparing: bool = False,
    operator_controls: bool = False,
    tickets: bool = False,
    activity: bool = False,
    files: bool = False,
) -> tuple[Grant, ...]:
    return core_grants(
        preparing=preparing,
        operator_controls=operator_controls,
        tickets=tickets,
        activity=activity,
        files=files,
    ) + operator_grants(operator_controls=operator_controls, tickets=tickets, activity=activity)


def host_grants(config: HostConfig) -> tuple[Grant, ...]:
    """The current work-scope matrix of a prepared host, from its explicit offline flags."""
    return work_grants(
        operator_controls=config.operator_controls,
        tickets=config.workplace_tickets,
        activity=config.live is not None and config.live.activity.enabled,
        files=config.workplace_files is not None,
    )


def ticket_readers() -> tuple[RecordAccess, ...]:
    """Ticket/message ACL: the operator reads them in the panel; nothing else is shared."""
    return (RecordAccess(OPERATOR.id, api_transfer=True),)


def settings_grants() -> tuple[Grant, ...]:
    """Configuration TEST scope has no model, jobs, channel or autonomous capability."""
    owners = (Owner.PERSONA, Owner.GM, Owner.WORKSPACE)
    return tuple(
        Grant(PRINCIPALS[o], o, read=True, write=True, api_transfer=o in TRANSFER_OWNERS)
        for o in owners
    ) + tuple(Grant(OPERATOR, o, read=True, api_transfer=True) for o in (*owners, Owner.MEMORY))


def lab_scope_grants() -> tuple[Grant, ...]:
    """TEST scope: Reports owners only, the laboratory's own principals, operator report reads."""
    own = (Owner.MEMORY, Owner.CATALOGUE, Owner.LABORATORY)
    core = tuple(
        Grant(PRINCIPALS[o], o, read=True, write=True, api_transfer=o in TRANSFER_OWNERS)
        for o in own
    ) + (Grant(PRINCIPALS[Owner.CATALOGUE], Owner.MEMORY, read=True),)
    operator = tuple(Grant(OPERATOR, o, read=True, api_transfer=True) for o in OPERATOR_LAB_OWNERS)
    return core + lab_grants() + operator


def record_readers() -> tuple[RecordAccess, ...]:
    """Record ACL for profile/state/GM/permission/source records created by trusted owners."""
    return (
        (RecordAccess(OPERATOR.id, api_transfer=True),)
        + tuple(
            RecordAccess(PRINCIPALS[o].id, api_transfer=o in TRANSFER_OWNERS)
            for o in (
                Owner.GM,
                Owner.PERSONA,
                Owner.MEMORY,
                Owner.JOBS,
                Owner.BEHAVIOR,
                Owner.GATEWAY,
            )
        )
        + (RecordAccess(PRINCIPALS[Owner.SOCIAL].id),)  # read only, never API transfer
        # Read only: the file release guard, skill evidence and attempt inputs check a source.
        # Without the offline workplace-files grants this entry opens nothing.
        + tuple(
            RecordAccess(PRINCIPALS[o].id) for o in (Owner.CATALOGUE, Owner.SKILLS, Owner.ACTIVITY)
        )
    )


def report_readers() -> tuple[RecordAccess, ...]:
    return (
        RecordAccess(PRINCIPALS[Owner.CATALOGUE].id),
        RecordAccess(OPERATOR.id, api_transfer=True),
    )
