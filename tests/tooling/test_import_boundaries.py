"""Exercise the architectural check on prohibited and allowed imports."""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "check_imports", Path(__file__).resolve().parents[2] / "scripts/check_imports.py"
)
assert SPEC is not None and SPEC.loader is not None
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


@pytest.mark.parametrize(
    ("layer", "source", "blocked"),
    [
        ("ai_agent_domain", "from ai_agent_adapters.sqlite import Store", True),
        ("ai_agent_contracts", "import ai_agent_core.bootstrap as core", True),
        ("ai_agent_application", "import ai_agent_worker.bootstrap", True),
        ("ai_agent_application", "from ai_agent_domain import State", False),
        ("ai_agent_adapters", "from ai_agent_application import Port", False),
    ],
)
def test_dependency_direction(layer: str, source: str, blocked: bool) -> None:
    assert bool(CHECK.violations(source, layer)) is blocked
