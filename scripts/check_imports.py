"""Check direct internal imports against the dependency direction in doc23."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAYERS = {
    "ai_agent_contracts": frozenset({"ai_agent_contracts"}),
    "ai_agent_domain": frozenset({"ai_agent_domain"}),
    "ai_agent_application": frozenset(
        {"ai_agent_application", "ai_agent_domain", "ai_agent_contracts"}
    ),
    "ai_agent_adapters": frozenset(
        {"ai_agent_adapters", "ai_agent_application", "ai_agent_domain", "ai_agent_contracts"}
    ),
}


def violations(source: str, layer: str) -> list[str]:
    """Return forbidden absolute internal imports; relative imports stay in their package."""
    blocked = []
    for node in ast.walk(ast.parse(source)):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            modules = [node.module]
        for module in modules:
            top = module.split(".")[0]
            if top.startswith("ai_agent_") and top not in LAYERS[layer]:
                blocked.append(module)
    return blocked


def main() -> None:
    """Check only repository-owned Python packages, excluding local runtime files."""
    errors = []
    for source in (ROOT / "packages").rglob("*.py"):
        layer = next((name for name in LAYERS if name in source.parts), None)
        if layer is not None:
            for module in violations(source.read_text(encoding="utf-8"), layer):
                errors.append(f"{source.relative_to(ROOT)}: forbidden import {module}")
    if errors:
        raise SystemExit("\n".join(errors))
    print("Internal import boundaries OK (direct static imports).")


if __name__ == "__main__":
    main()
