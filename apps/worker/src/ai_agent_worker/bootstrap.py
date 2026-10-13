"""Worker configuration check without a broker connection or execution loop."""

from ai_agent_adapters.configuration import (
    BootstrapSettings,
    ConfigurationError,
    StartupReceipt,
    fresh_startup,
    from_environment,
    validate_bindings,
)


def create_worker_bootstrap(
    settings: BootstrapSettings | None = None, *, previous_state: str | None = None
) -> StartupReceipt:
    """Validate the same config adapter; never restore an active runtime."""
    configuration = from_environment() if settings is None else settings
    validate_bindings(configuration)
    return fresh_startup(previous_state)


def main() -> None:
    """Verify configuration without jobs, model loading or a web listener."""
    try:
        receipt = create_worker_bootstrap()
    except ConfigurationError as error:
        print(str(error))
        raise SystemExit(78) from None
    print(f"Worker bootstrap {receipt.state}; no jobs started.")
