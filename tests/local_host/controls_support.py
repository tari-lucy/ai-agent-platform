"""Synthetic evidence via actual owners over init-created grants; never provisions test grants."""

from datetime import UTC, datetime

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.development import Candidate, DevelopmentController, MemoryEvidence
from ai_agent_application.dialogue import ControlSnapshot
from ai_agent_application.memory import EpisodeDraft, MemoryService, SourceDraft
from ai_agent_application.storage import Owner
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.persona import CharacterState
from ai_agent_contracts.states import ActivityState, Provenance, RunState

from ai_agent_core.local_host import rights
from ai_agent_core.local_host.init import work_scope
from ai_agent_core.local_host.layout import load_config, paths
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS, command


async def seed_change(root):
    host = paths(root)
    config = load_config(host)
    scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
    registry = new_registry()
    store = SQLiteStore(host.database, extensions=registry)
    try:
        memory = MemoryService(
            store, PRINCIPALS[Owner.MEMORY], registry, reader_access=rights.record_readers()
        )
        made = await memory.register_source(
            command(scope, "controls-source"),
            SourceDraft(
                provenance=Provenance.OBSERVATION,
                source_type="synthetic-observation",
                adapter_id="synthetic",
                adapter_version="1",
                occurred_at=datetime.now(UTC),
                content="Вымышленное наблюдение для приёмки.",
                read_allowed=True,
                api_transfer_allowed=True,
                public_disclosure_allowed=False,
            ),
        )
        ref = SourceRef(
            source_id=made.source_id, version=made.version, scope=scope, availability="available"
        )
        episode, _ = await memory.record_episode(
            command(scope, "controls-episode"),
            EpisodeDraft(
                sources=(ref,),
                participant_ids=(),
                choice="Уточнила условие.",
                actual_outcome="Получила ответ.",
                operation_id="controls-observation",
                provenance="observation",
            ),
        )
        controller = DevelopmentController(
            store,
            PRINCIPALS[Owner.PERSONA],
            registry,
            MemoryEvidence(memory),
            lambda: ControlSnapshot(RunState.STOPPED, ActivityState.OPEN, "observation", False),
            reader_access=rights.record_readers(),
        )
        state = await store.repository(CharacterState, PRINCIPALS[Owner.PERSONA], scope).get(
            "state:" + scope.persona_id
        )
        outcome = await controller.submit(
            scope,
            Candidate(
                proposal_id="controls-proposal",
                property="openness",
                new_value=0.6,
                interpretation="Уточнение помогло.",
                alternative="Возможно, совпадение.",
                uncertainty=0.2,
                expected_manifestation="Чаще уточняет условия.",
                episode_ids=(episode,),
                base_version=state.version,
            ),
        )
        assert outcome.change.status.value == "AWAITING_APPROVAL"
        return outcome.change
    finally:
        await store.close()


def grants_snapshot(root):
    # Inspect only technical grants in the synthetic database, without write PRAGMAs.
    import sqlite3
    from contextlib import closing

    from ai_agent_adapters.storage import schema

    with closing(
        sqlite3.connect(paths(root).database.as_uri() + "?mode=ro", uri=True)
    ) as connection:
        return sorted(connection.execute("SELECT * FROM " + schema.grants.name).fetchall())
