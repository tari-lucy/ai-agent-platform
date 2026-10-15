"""Explicit ASGI factory for a prepared database and trusted provider adapters."""

from ai_agent_adapters.configuration import BootstrapSettings
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.control import Supervisor
from ai_agent_application.gamemaster import Family
from ai_agent_application.laboratory import LabRunner
from ai_agent_application.llm import ProviderFactory
from ai_agent_application.memory.ports import RecallIndex
from ai_agent_application.scheduling import ResourceScheduler
from ai_agent_application.storage import RecordAccess
from ai_agent_contracts.common import Scope
from ai_agent_contracts.extensions import ExtensionRegistry
from fastapi import FastAPI

from ai_agent_core.bootstrap import create_app

from .artifacts import Reports
from .service import PrivateAPIBudget, Runtime


def create_m3_app(
    settings: BootstrapSettings,
    store: SQLiteStore,
    registry: ExtensionRegistry,
    *,
    providers: ProviderFactory,
    scopes: tuple[Scope, ...],
    allowed_adapters: frozenset[str] = frozenset({"mock"}),
    families: tuple[Family, ...] = (),
    lab: LabRunner | None = None,
    reports: Reports | None = None,
    reader_access: tuple[RecordAccess, ...] = (),
    dialogue_index: RecallIndex | None = None,
    private_api_budget: PrivateAPIBudget | None = None,
) -> FastAPI:
    if not scopes:
        raise ValueError("M3_SCOPES")

    def compose(supervisor: Supervisor, scheduler: ResourceScheduler) -> Runtime:
        return Runtime(
            store,
            registry,
            supervisor,
            scheduler,
            providers,
            scopes=scopes,
            allowed_adapters=allowed_adapters,
            families=families,
            lab=lab,
            reports=reports,
            reader_access=reader_access,
            dialogue_index=dialogue_index,
            private_api_budget=private_api_budget,
        )

    return create_app(settings, runtime_factory=compose, base_scope=scopes[0])
