"""Synthetic session host fixtures shared by runtime and browser journeys.

No production setup or rights: callers explicitly seed their own disposable host.
"""

from datetime import timedelta

from ai_agent_application.agent_workplace.ports import HarnessEvent, ModelRequest
from ai_agent_application.llm import ChatMessage
from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_workplace import CapabilityBinding, CapabilityManifest
from ai_agent_contracts.api import Permission
from test_activity import ACTIVITY_MARKER
from test_http import post

from ai_agent_core.m3_composition.state import extension, put, record

CAPABILITY = "workplace.synthetic-session"


async def install(rt, scope, *, capability=CAPABILITY, areas=None):
    now = rt.supervisor.clock.utc()
    common = dict(scope=scope, version=1, created_at=now, updated_at=now)
    manifest = CapabilityManifest(
        id="session-manifest",
        **common,
        capability_id=capability,
        capability_version=1,
        summary="Synthetic session fixture",
        instructions="Synthetic only",
        input_schema_id="session.instruction",
        output_schema_id="session.result",
        effect="local_write",
        areas=areas or ["inbox"],
        network="gateway",
        runner_required=True,
    )
    binding = CapabilityBinding(
        id="session-binding",
        binding_id="session-binding",
        **common,
        capability_id=capability,
        capability_version=1,
        adapter_id="synthetic",
        adapter_version="1",
        state="ready",
        verified_at=now,
    )
    permission = record(
        rt.registry,
        Permission,
        id="session-permission",
        **common,
        principal_id="workplace-agent",
        capability_id=capability,
        operation_type=capability,
        binding_id=binding.id,
        level="automatic",
        conditions=extension(rt.registry, "m3.empty", {})
        if areas is None
        else extension(
            rt.registry,
            "workplace.permission-conditions",
            dict(
                areas=areas, constraints="Synthetic test only", ticket_id=None, ticket_version=None
            ),
        ),
        limits=extension(rt.registry, "m3.empty", {}),
        policy_version=1,
        author_id="operator",
        expires_at=now + timedelta(days=1),
    )
    for item in (manifest, binding, permission):
        await put(
            rt.store, rt.registry, Owner.WORKSPACE, scope, "seed-" + item.id, item, None, rt.readers
        )

    synthetic(rt, capability)


def synthetic(rt, capability=CAPABILITY):
    """Only the scripted harness over the shared gateway; no records or rights are seeded."""

    class Harness:
        def __init__(self, gateway):
            self.gateway = gateway

        async def turn(self, gate, turn):
            reply = await self.gateway.generate(
                gate,
                ModelRequest(
                    "activity",
                    (ChatMessage("system", ACTIVITY_MARKER), ChatMessage("user", turn.instruction)),
                    max_output_tokens=64,
                    call_id="session-answer",
                ),
            )
            return [HarnessEvent("result", reply.text)] if reply.status == "completed" else []

        async def close(self, key):
            pass

        async def cancel(self, key):
            pass

        async def staged(self, gate, output):
            raise AssertionError("Synthetic harness has no staged files")

    async def factory(execution, gateway, gate):
        return Harness(gateway)

    rt.session_harness = factory
    rt.session_capability = capability


def ack(host, draft, key="work-ack"):
    return post(
        host.env,
        dict(
            type="dialogue.acknowledge",
            request_id=key,
            scope=draft["scope"],
            target=draft["utterance_id"],
            expected_version=draft["version"],
            arguments={"revision": draft["revision"]},
        ),
    )
