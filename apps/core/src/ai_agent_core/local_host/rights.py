"""The explicit rights matrix of a local host. `init` provisions it; `serve` never changes it.

Each row names the owner read/write that needs it. MODERATION and public disclosure are absent:
no public output exists in this host, and nothing is granted "to everyone".

The set is the smallest one that the covered flow passed with (see tests/local_host): login,
private dialogue with acknowledgment, TEST laboratory and report. Rows for SOCIAL, PERSONA/GM
reading MEMORY, LLM reading WORKSPACE and CATALOGUE reading MEMORY (work scope) were removed after
the same flow still passed without them; JOBS->BEHAVIOR and BEHAVIOR/JOBS/GATEWAY->MEMORY were
each shown necessary by a failing run. Paths this host does not run yet (extract, consolidate, gm
and other jobs, which the mock leaves unanswered) get their reads only with their own acceptance:
until then they are denied, not widened.
"""

from ai_agent_application.laboratory import lab_grants
from ai_agent_application.storage import Grant, Owner, RecordAccess

from ai_agent_core.m3_composition.state import OPERATOR, PRINCIPALS

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
)
# Only these owners' records may be handed on over the loopback JSON transfer.
TRANSFER_OWNERS = (Owner.MEMORY, Owner.PERSONA, Owner.GM)
# Operator reads over HTTP (private dialogue, jobs, usage), with explicit api_transfer.
OPERATOR_OWNERS = (
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


def core_grants(*, preparing: bool = False) -> tuple[Grant, ...]:
    """WORKSPACE (Capability/Permission/Connection) is written only while `init` prepares it."""
    grants = [
        Grant(
            PRINCIPALS[o],
            o,
            read=True,
            write=preparing or o is not Owner.WORKSPACE,
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
    ]
    return tuple(grants)


def operator_grants() -> tuple[Grant, ...]:
    return tuple(Grant(OPERATOR, o, read=True, api_transfer=True) for o in OPERATOR_OWNERS)


def work_grants(*, preparing: bool = False) -> tuple[Grant, ...]:
    return core_grants(preparing=preparing) + operator_grants()


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
    return (RecordAccess(OPERATOR.id, api_transfer=True),) + tuple(
        RecordAccess(PRINCIPALS[o].id, api_transfer=o in TRANSFER_OWNERS)
        for o in (Owner.GM, Owner.PERSONA, Owner.MEMORY, Owner.JOBS, Owner.BEHAVIOR, Owner.GATEWAY)
    )


def report_readers() -> tuple[RecordAccess, ...]:
    return (
        RecordAccess(PRINCIPALS[Owner.CATALOGUE].id),
        RecordAccess(OPERATOR.id, api_transfer=True),
    )
