"""The memory contract for harness conversations (doc33 stage 3, D82).

A learning owner records each harness message as a Source of one of ``SOURCE_TYPES`` through
the memory owner, and groups them in an Episode whose ``operation_id`` starts with
``OPERATION``. There is no dialogue Job behind such an Episode: ``memory_cycles.pairs`` checks
its sources directly (current version, content, rights for api_transfer).
"""

OPERATION = "harness-"
SOURCE_TYPES = frozenset({"harness-operator-message", "harness-agent-message"})


def is_harness(operation_id: str) -> bool:
    return operation_id.startswith(OPERATION)
