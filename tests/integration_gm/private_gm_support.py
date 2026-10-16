"""Synthetic GM-aware model and a prepared host with the game master explicitly enabled."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from ai_agent_application.memory.service import derived_id
from ai_agent_application.storage import Owner
from ai_agent_contracts.persona import GmRuntimeState, Opportunity, StoryArc
from quality_support import PUBLIC_PROFILE, Host, Model
from test_http import post

from ai_agent_core.local_host.gm import configure_gm
from ai_agent_core.local_host.init import initialise
from ai_agent_core.local_host.layout import LiveConfig
from ai_agent_core.m3_composition.service import gm_request_id
from ai_agent_core.m3_composition.state import PRINCIPALS

LINE = "Опечатка расследует, почему в редакции пропадают запятые."
BOUNDS = "Только разговор в приватной панели; никаких публикаций и встреч."
ROLES = (
    ("Ты внутренний гейммастер", "game-master"),
    ("Проверь только данные на сюжетные ограничения", "story-screen"),
    ("Ты извлекаешь проверяемые утверждения", "extractor"),
    ("Кратко перескажи разговор", "consolidator"),
    ("осмыслить пережитое", "reflection"),
)
CLEAR = dict(hard=[], soft=[], uncertain=False)
DRAFT = dict(
    applicability="Можно спросить собеседника, замечал ли он пропавшие запятые в письмах.",
    boundaries="Только вопрос в разговоре; ответ собеседника не известен заранее.",
    topic_key="lost-commas-question",
)


class GmModel(Model):
    """Answers by role; `hold` delays one role until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        for role in ("game-master", "story-screen"):
            self.queues[role] = []
        self.hold: dict[str, asyncio.Event] = {}

    @staticmethod
    def role(body: dict) -> str:
        system = body["messages"][0]["content"]
        return next((role for marker, role in ROLES if marker in system), "character")

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        role = self.role(json.loads(request.content))
        gate = self.hold.get(role)
        if gate is not None:
            self.waiting = True
            await gate.wait()
        return await super().__call__(request)


async def gm_host(tmp_path: Path, model: GmModel, monkeypatch, **gm: object) -> Host:
    root = tmp_path / "host"
    await initialise(
        root,
        profile_markdown=PUBLIC_PROFILE.read_text(encoding="utf-8"),
        operator_controls=True,
        live=LiveConfig(model_id="fixture-model", attempt_limit=40, attempt_period="utc_day"),
    )
    if gm:
        await configure_gm(root, **gm)
    return Host(root, tmp_path, model, monkeypatch)


async def unpause(host: Host) -> None:
    """The existing operator control: GM starts paused in every new run."""
    rt = host.runtime
    state = await rt.store.repository(GmRuntimeState, PRINCIPALS[Owner.GM], host.scope).get(
        "gm-runtime"
    )
    run = rt.supervisor.snapshot
    made = await post(
        host.env,
        dict(
            type="gm.configure",
            request_id="unpause-" + run.run_id,
            target="gm-runtime",
            scope=run.scope.model_dump(mode="json"),
            expected_version=state.version,
            arguments=dict(
                schema_id="gm.configure-args", schema_version="1.0", data={"paused": False}
            ),
        ),
    )
    assert made.status_code == 200, made.text
    assert not rt.supervisor.snapshot.gm_paused


async def due(host: Host) -> str:
    """Pretend the configured active interval has passed, then run one GM step."""
    gm = host.runtime.autonomous_gm
    gm.next_check, gm.next_active = 0.0, 0.0
    return await gm.tick()


def slot_operation(host: Host, slot: int = 1) -> str:
    scope = host.runtime.supervisor.snapshot.scope
    return derived_id("m3job", scope, gm_request_id(datetime.now(UTC).date(), slot))


async def offers(host: Host) -> list[Opportunity]:
    return await host.runtime.store.repository(
        Opportunity, PRINCIPALS[Owner.GM], host.scope
    ).list_for_lifecycle(limit=200)


async def lines(host: Host) -> list[StoryArc]:
    return await host.runtime.store.repository(StoryArc, PRINCIPALS[Owner.GM], host.scope).list(
        limit=200
    )
