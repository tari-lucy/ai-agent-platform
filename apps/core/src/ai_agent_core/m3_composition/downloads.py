"""Full report download through the existing laboratory/catalogue owners."""

from ai_agent_application.storage import Principal
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import ArtifactRef, Scope
from ai_agent_contracts.states import ErrorCode
from ai_agent_contracts.workspace import LaboratoryReport

from .service import Runtime


class Downloads:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    async def read(
        self, principal_id: str, scope: Scope, report_id: str
    ) -> tuple[ArtifactRef, bytes]:
        rt = self.runtime
        if scope.memory_mode != "test" or scope.partition not in {s.partition for s in rt.scopes}:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if rt.reports is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        principal = Principal(principal_id, "operator")
        repository = rt.store.repository(LaboratoryReport, principal, scope)
        report = await repository.get(report_id, purpose="api_transfer")
        if report is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        reference = report.report_artifact
        if reference is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        if reference.media_type != "application/json" or not 0 < reference.size_bytes <= 2_000_000:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        body = await rt.reports.read(
            scope, reference.artifact_id, principal, purpose="api_transfer", expected=reference
        )
        again = await repository.get(report_id, purpose="api_transfer")
        if again is None or again != report:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        return reference, body
