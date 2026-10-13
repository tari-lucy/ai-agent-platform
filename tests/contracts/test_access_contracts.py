"""Auth additions remain bounded and canonical across offline exports."""

import pytest
from ai_agent_contracts.access import OperatorLogin, ServiceClaimReceipt
from ai_agent_contracts.codec import ContractError, from_wire
from ai_agent_contracts.common import Scope
from ai_agent_contracts.export import openapi
from pydantic import TypeAdapter


def test_login_is_secret_write_only_and_rejects_unbounded_or_extra_values():
    schema = OperatorLogin.model_json_schema()
    assert schema["properties"]["password"]["writeOnly"] is True
    assert "synthetic-password" not in repr(OperatorLogin(password="synthetic-password"))
    for data in (
        b'{"password":123}',
        b'{"password":"","extra":"x"}',
        ('{"password":"' + "x" * 1025 + '"}').encode(),
    ):
        with pytest.raises(ContractError):
            from_wire(TypeAdapter(OperatorLogin), data)


def test_empty_claim_cannot_grant_execution():
    scope = Scope(workspace_id="w", persona_id="p", memory_space_id="m", memory_mode="test")
    with pytest.raises(ValueError):
        ServiceClaimReceipt(request_id="r", scope=scope, command=None, lease=None, acquired=True)


def test_auth_and_service_exports_are_canonical():
    paths = openapi()["paths"]
    assert paths["/api/v1/auth/login"]["post"]["security"] == []
    assert paths["/api/v1/auth/session"]["get"]["security"] == [{"OperatorSession": []}]
    assert paths["/api/v1/auth/logout"]["post"]["security"] == [{"OperatorSession": [], "CSRF": []}]
    responses = paths["/internal/v1/jobs/{operation_id}/claim"]["post"]["responses"]
    assert responses["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ServiceClaimReceipt"
    )
