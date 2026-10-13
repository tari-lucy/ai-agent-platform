"""Worker package check without an execution loop."""

from importlib.metadata import version


def main() -> None:
    """Verify installed metadata without connecting to any service."""
    print(f"Worker bootstrap {version('ai-agent-platform')} OK; no jobs started.")
