"""Only synthetic private files; setup never replaces an existing binding."""

import os
import stat

import pytest
from access_support import configured_hash
from ai_agent_adapters.configuration import load_configuration

from ai_agent_core.access.setup import write_binding


def test_exclusive_private_binding_roundtrip_and_no_overwrite(tmp_path, capsys):
    path = tmp_path / ".env.access"
    write_binding(path, configured_hash())
    settings = load_configuration(env_file=path, environ={})
    assert settings.operator_password_hash == configured_hash()
    assert len(settings.worker_service_token.get_secret_value()) == 64
    with pytest.raises(FileExistsError):
        write_binding(path, configured_hash())
    assert (
        load_configuration(env_file=path, environ={}).worker_service_token
        == settings.worker_service_token
    )
    assert capsys.readouterr().out == ""
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
