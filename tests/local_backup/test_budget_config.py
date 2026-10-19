"""Backup configuration reader preserves a committed cap receipt with strict UTC dates."""

from datetime import UTC, datetime, timedelta

from ai_agent_contracts.common import Scope
from ai_agent_contracts.panel import ApiLimitChange

from ai_agent_core.local_backup.compat import _read_config
from ai_agent_core.local_host.layout import HostConfig, LiveConfig


def test_config_with_budget_receipt_is_readable_by_backup(tmp_path):
    now = datetime.now(UTC)
    scope = Scope(
        workspace_id="synthetic", persona_id="alpha", memory_space_id="work", memory_mode="work"
    )
    result = ApiLimitChange(
        id="change",
        scope=scope,
        version=1,
        created_at=now,
        updated_at=now,
        operator_id="operator",
        request_id="cap",
        fingerprint="a" * 64,
        attempt_limit=350,
        period="utc_day",
        budget_version=2,
        expires_at=now + timedelta(days=30),
    )
    config = HostConfig(
        workspace_id="synthetic",
        persona_id="alpha",
        work_space_id="work",
        test_space_ids=("test",),
        port=1024,
        permission_expires_at="2099-01-01T00:00:00Z",
        operator_controls=True,
        settings_space_id="settings",
        live=LiveConfig(
            model_id="m",
            attempt_limit=350,
            attempt_period="utc_day",
            budget_version=2,
            budget_receipt=result,
        ),
    )
    path = tmp_path / "host-config.json"
    path.write_text(config.model_dump_json())
    assert _read_config(path) == config
