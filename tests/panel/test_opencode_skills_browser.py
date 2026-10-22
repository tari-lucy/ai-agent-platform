"""Real browser + real OpenCode/Docker + production owners, scripted model only.

Reuses Claude's loopback browser/server fixture without changing its file. The explicit
test-only installer replaces its synthetic harness; it is not production launcher wiring.
"""

import asyncio
import json
import os

import httpx
import pytest
import test_work_conversation_ack_browser as support
from ai_agent_adapters.agent_harness.opencode import PREFIX
from ai_agent_adapters.sandbox.policy import RunnerTemplate
from ai_agent_application.agent_workplace.ports import RunnerLimits
from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_workplace import WorkAttempt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.llm import LLMAttempt
from ai_agent_contracts.states import ErrorCode
from playwright.sync_api import expect
from session_support import install
from test_activity import WorkModel
from test_live import answer
from test_work_conversation_ack_browser import work_host as work_host

from ai_agent_core.local_host.layout import paths
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.state import PRINCIPALS
from ai_agent_core.m3_composition.work_session_opencode import OpenCodeSessions

pytestmark = pytest.mark.skipif(
    not os.environ.get("AW_SKILLS_IMAGE_ID"), reason="explicit real OpenCode skill image required"
)
CAPABILITY = "workplace.opencode-session"
METHOD = "1. Outline the synthetic lighthouse."
IMPROVED = METHOD + "\n2. Mark the light and the shadow."
REPLIES = ("Метод сохранён.", "Нашла и прочитала свой метод.", "Добавила второй шаг в метод.")


class SkillModel(WorkModel):
    @staticmethod
    def tool_result(body):
        content = body["messages"][-1]["content"]
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content)
        return json.loads(content)

    async def __call__(self, request):
        body = json.loads(request.content)
        self.calls.append(("activity", body))
        number = len(self.calls)
        tool = next(
            t["function"] for t in body["tools"] if t["function"]["name"] == "platform_workspace"
        )
        assert {"save_skill", "read_skill", "find_skills"} <= set(
            tool["parameters"]["properties"]["operation"]["enum"]
        )
        if number in (2, 5, 7):
            result = self.tool_result(body)
            if number in (2, 7):
                assert result == {
                    "skill_id": "lighthouse",
                    "version": 1 if number == 2 else 2,
                    "status": "experimental",
                }
            else:
                assert result["method"] == METHOD
            text = REPLIES[{2: 0, 5: 1, 7: 2}[number]]
            return httpx.Response(
                200, content=answer(text), headers={"Content-Type": "text/event-stream"}
            )
        if number in (1, 6):
            operation = "save_skill"
            arguments = dict(
                skill_id="lighthouse",
                title="Synthetic lighthouse method",
                applicability="Draw a synthetic lighthouse.",
                method=METHOD if number == 1 else IMPROVED,
            )
            if number == 6:
                arguments["expected_version"] = "1"
                arguments["change_reason"] = "Add a shadow step."
        elif number == 3:
            operation, arguments = "find_skills", dict(query="lighthouse")
        elif number == 4:
            assert self.tool_result(body)[0]["skill_id"] == "lighthouse"
            operation, arguments = "read_skill", dict(skill_id="lighthouse")
        else:
            raise AssertionError("Unexpected model call")
        call = dict(
            index=0,
            id=f"skill-{number}",
            type="function",
            function=dict(
                name="platform_workspace",
                arguments=json.dumps(dict(operation=operation, arguments=arguments)),
            ),
        )
        rows = [
            {"choices": [{"delta": {"tool_calls": [call]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {
                "choices": [],
                "usage": {"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38},
            },
        ]
        wire = "".join("data: " + json.dumps(row) + "\n\n" for row in rows) + "data: [DONE]\n\n"
        return httpx.Response(
            200, content=wire.encode(), headers={"Content-Type": "text/event-stream"}
        )


@pytest.fixture
def opencode_setup(tmp_path, monkeypatch):
    """Only fixture composition changes; real local_host/launcher files are Claude's zone."""
    model, shared = SkillModel(), {}
    prepare = support.prepared

    async def prepared(directory, patch, provider):
        host = await prepare(directory, patch, provider)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await enable_workplace_files(host.root)
        return host

    async def real_install(rt, scope):
        await install(rt, scope, capability=CAPABILITY, areas=["skills", "projects"])
        template = RunnerTemplate(
            CAPABILITY,
            os.environ["AW_SKILLS_IMAGE_ID"],
            PREFIX,
            RunnerLimits(20, 1024, 128, 55, 65536, 32 * 1024 * 1024),
            65536,
            max_files=8,
            model_role_id="activity",
            max_model_calls=3,
            max_prompt_chars=100000,
            max_model_tokens=512,
            max_workspace_calls=3,
        )
        rt.session_harness = None
        controller = OpenCodeSessions(rt, template, output_area="projects")
        controller.install()
        failures = []
        perform = rt._perform

        async def observed(ex):
            try:
                return await perform(ex)
            except Exception as error:
                if not (
                    isinstance(error, ContractError) and error.error.code == ErrorCode.STALE_CONTEXT
                ):
                    failures.append(error)
                raise

        rt._perform = observed
        shared.update(
            rt=rt, loop=asyncio.get_running_loop(), controller=controller, failures=failures
        )

    monkeypatch.setattr(support, "WorkModel", lambda: model)
    monkeypatch.setattr(support, "prepared", prepared)
    monkeypatch.setattr(support, "install", real_install)
    return shared


@pytest.fixture
def opencode_host(opencode_setup, work_host):
    return work_host, opencode_setup


def test_real_opencode_saves_recalls_improves_skill_and_panel_acknowledges(page, opencode_host):
    (origin, host, model), shared = opencode_host
    page.goto(origin + "/#/work")
    page.get_by_label("Пароль", exact=True).fill(paths(host.root).password.read_text().strip())
    page.get_by_role("button", name="Войти", exact=True).click()
    talk = page.get_by_label("Разговор о работе")
    talk.get_by_label("Новый разговор о работе").fill("Синтетический метод")
    talk.get_by_role("button", name="Начать разговор").click()

    def conversation():
        return page.get_by_label("Разговор: Синтетический метод")

    messages = conversation().get_by_role("list", name="Сообщения разговора")
    for number, instruction in enumerate(
        (
            "Сохрани метод наброска маяка.",
            "Найди и прочитай свой метод.",
            "Улучши метод вторым шагом.",
        )
    ):
        conversation().get_by_label("Сообщение агенту").fill(instruction)
        conversation().get_by_role("button", name="Отправить").click()
        if number == 0:
            expect(
                conversation().get_by_text("сохранено, ждёт исполнения", exact=False)
            ).to_be_visible()
            assert model.count("activity") == 0
            page.get_by_role("button", name="START — запустить агента").click()
        try:
            expect(messages.get_by_text(REPLIES[number], exact=True)).to_be_visible(timeout=70000)
        except Exception:
            if shared["failures"]:
                raise shared["failures"][0] from None
            raise
        expect(page.get_by_label("Ответ агента, ожидающий получения")).to_have_count(0)
        expect(conversation().get_by_text("сохранено, ждёт исполнения", exact=False)).to_have_count(
            0
        )
        expect(messages.get_by_role("listitem")).to_have_count(2 * (number + 1))
        assert model.count("activity") == (2, 5, 7)[number]

    async def state():
        rt = shared["rt"]
        skill = await rt.workplace.skills.get(host.scope, "lighthouse")
        original = await rt.store.repository(type(skill), PRINCIPALS[Owner.SKILLS], host.scope).get(
            skill.id, version=1
        )
        _, content = await rt.workplace.files.released_content(
            host.scope, skill.method.artifact_id, "api_transfer"
        )
        attempts = await rt.store.repository(
            WorkAttempt, PRINCIPALS[Owner.ACTIVITY], host.scope
        ).list(limit=50)
        calls = await rt.store.repository(LLMAttempt, PRINCIPALS[Owner.LLM], host.scope).list(
            limit=50
        )
        assert (
            skill.version == 2 and skill.status == "experimental" and content.decode() == IMPROVED
        )
        assert any(original.method in attempt.input_files for attempt in attempts)
        assert any(skill.method in attempt.result_files for attempt in attempts)
        assert len(calls) == 7 and all(c.role_id == "activity" for c in calls)
        assert not shared["controller"].resources and not shared["controller"].runners

    asyncio.run_coroutine_threadsafe(state(), shared["loop"]).result(15)
    page.reload()
    messages = conversation().get_by_role("list", name="Сообщения разговора")
    expect(messages.get_by_role("listitem")).to_have_count(6)
    for text in REPLIES:
        expect(messages.get_by_text(text, exact=True)).to_have_count(1)
    page.get_by_role("button", name="STOP — остановить активного агента").click()
    expect(page.get_by_role("button", name="START — запустить агента")).to_be_visible(timeout=15000)
    assert model.count("activity") == 7 and not shared["failures"]
