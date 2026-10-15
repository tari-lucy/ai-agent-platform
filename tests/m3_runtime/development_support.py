"""Real owner-backed synthetic operator changes for HTTP and browser acceptance."""

from contextlib import asynccontextmanager

from ai_agent_application.development import Candidate
from ai_agent_application.memory import EpisodeDraft, MemoryService, SourceDraft
from ai_agent_application.storage import Grant, Owner, Principal
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.persona import CharacterState, StateChange
from ai_agent_contracts.states import Provenance
from transfer_support import prepared as transfer_prepared

from ai_agent_core.m3_composition.development_controls import controller
from ai_agent_core.m3_composition.state import PRINCIPALS, command


@asynccontextmanager
async def prepared(tmp_path, **options):
    async with transfer_prepared(tmp_path, **options) as env:
        env.fixture_grants += (
            Grant(Principal("operator", "operator"), Owner.MEMORY, read=True, api_transfer=True),
        )
        await env.world.store.provision(env.target, env.fixture_grants)
        memory = MemoryService(
            env.world.store,
            PRINCIPALS[Owner.MEMORY],
            env.runtime.registry,
            clock=env.world.clock,
            reader_access=env.readers,
        )
        result = await memory.register_source(
            command(env.target, "development-source"),
            SourceDraft(
                provenance=Provenance.OBSERVATION,
                source_type="operator-test",
                adapter_id="synthetic",
                adapter_version="1",
                occurred_at=env.world.clock(),
                content="Вымышленный пережитый опыт.",
                read_allowed=True,
                api_transfer_allowed=True,
                public_disclosure_allowed=False,
            ),
        )
        env.source_ref = SourceRef(
            source_id=result.source_id,
            version=result.version,
            scope=env.target,
            availability="available",
        )
        episode_id, _ = await memory.record_episode(
            command(env.target, "development-episode"),
            EpisodeDraft(
                sources=(env.source_ref,),
                participant_ids=(),
                choice="Уточнила условия.",
                actual_outcome="Получила ответ.",
                operation_id="development-experience",
                provenance="observation",
            ),
        )
        env.development = controller(env.runtime)
        candidate = Candidate(
            proposal_id="operator-proposal",
            property="openness",
            new_value=0.6,
            interpretation="Опыт показал пользу уточнения.",
            alternative="Возможно, совпадение.",
            uncertainty=0.2,
            expected_manifestation="Чаще уточняет условия.",
            episode_ids=(episode_id,),
            base_version=2,
        )
        env.change = (await env.development.submit(env.target, candidate)).change
        assert env.change.status.value == "AWAITING_APPROVAL"

        async def get(model, identifier):
            return await env.world.store.repository(
                model, PRINCIPALS[Owner.PERSONA], env.target
            ).get(identifier)

        env.get = get
        env.state = lambda: get(CharacterState, "state:" + env.target.persona_id)
        env.current_change = lambda: get(StateChange, env.change.id)
        yield env


def edit(env, kind="change.decision", request="decision", **data):
    return dict(
        type=kind,
        request_id=request,
        target=env.change.id,
        scope=env.target.model_dump(mode="json"),
        expected_version=env.change.version,
        arguments=dict(
            schema_id=kind + "-args",
            data=data or dict(decision="accept", reason="Подтверждено пережитым опытом."),
        ),
    )


def rollback_preview(env, change):
    return dict(
        type="change.rollback.preview",
        request_id="rollback-preview",
        target=change.id,
        scope=env.target.model_dump(mode="json"),
        expected_version=change.version,
        arguments=dict(schema_id="change.rollback-args", data={}),
    )


def rollback_execute(env, plan):
    return dict(
        type="change.rollback.execute",
        request_id="rollback-execute",
        target=env.change.id,
        scope=env.target.model_dump(mode="json"),
        expected_version=plan["version"],
        arguments=dict(plan_id=plan["resource_id"], plan_version=plan["version"]),
    )
