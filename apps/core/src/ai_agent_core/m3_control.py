"""Bind M3 autonomy to one existing Supervisor cancellation identity.

Composition issues an Admission per job and supplies this binding to M3 services.
This helper never grants rights, provisions storage or starts a background job.
"""

from ai_agent_application.control import Supervisor
from ai_agent_application.control.ports import Admission
from ai_agent_application.dialogue.model import ControlSnapshot
from ai_agent_contracts.codec import ContractError

ROLE_KINDS = {
    "extractor": "reflection",
    "consolidator": "reflection",
    "reflection": "reflection",
    "game-master": "gm",
}


class PinnedM3Control:
    def __init__(self, supervisor: Supervisor, admission: Admission, role: str) -> None:
        if role not in ROLE_KINDS or admission.kind != ROLE_KINDS[role]:
            raise ValueError("M3_ROLE_ADMISSION_MISMATCH")
        supervisor.validate(admission)
        self.supervisor, self.admission, self.role = supervisor, admission, role

    def admitted(self) -> bool:
        try:
            current = self.supervisor.resolve(
                self.admission.id,
                scope=self.admission.scope,
                freshness=self.admission.freshness,
            )
            return current.kind == ROLE_KINDS[self.role]
        except ContractError:
            return False

    def snapshot(self) -> ControlSnapshot:
        admitted = self.admitted()
        current = self.supervisor.snapshot
        return ControlSnapshot(current.state, current.activity, current.autonomy, admitted)
