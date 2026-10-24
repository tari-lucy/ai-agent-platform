"""Linux headless browser: the work conversation tells how its turn goes, from recorded facts.

Production host app in-process (shared fixture of the ACK journey), real SessionBook,
WorkAttempt, JobOwner and LLM journal; a scripted session harness over the shared model
gateway (synthetic transport, no network, no paid calls). Checked in a real browser: waiting for
START, running with confirmed model calls, an explicit error, an unconfirmed outcome after the
worker stopped renewing its lease, and a turn that saved a file. A reload sends nothing again.
"""

import asyncio
import re
from dataclasses import replace

import pytest
import test_work_conversation_ack_browser as support
from ai_agent_application.agent_workplace.ports import HarnessEvent, ModelRequest, WorkspaceRequest
from ai_agent_application.llm import ChatMessage
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode, OperationState
from playwright.sync_api import expect
from test_activity import ACTIVITY_MARKER, FAST
from test_work_conversation_ack_browser import work_host as work_host

from ai_agent_core.local_host.layout import paths
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.work_session_workspace import Workspace

REPLY = "Синтетический итог рабочего хода."


@pytest.fixture
def scripted(tmp_path, monkeypatch):
    prepare, install = support.prepared, support.install
    plan: dict = {}

    async def prepared(directory, patch, model):
        host = await prepare(directory, patch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await enable_workplace_files(host.root)
        return host

    async def scripted_install(rt, scope):
        await install(rt, scope, areas=["knowledge", "inbox"])
        rt.activity = replace(FAST, max_attempts_per_task=1)  # one turn per message here
        loop = asyncio.get_running_loop()
        plan.update(rt=rt, loop=loop, entered=asyncio.Event(), release=asyncio.Event())

        class Harness:
            def __init__(self, execution, gateway):
                self.execution, self.gateway = execution, gateway

            async def turn(self, gate, turn):
                reply = await self.gateway.generate(
                    gate,
                    ModelRequest(
                        "activity",
                        (ChatMessage("system", ACTIVITY_MARKER), ChatMessage("user", "Синтетика")),
                        max_output_tokens=64,
                        call_id="progress-answer",
                    ),
                )
                plan["entered"].set()
                if plan["mode"] == "file":
                    await Workspace(self.execution, gate).call(
                        gate,
                        WorkspaceRequest(
                            "note",
                            "write_note",
                            dict(
                                file_id="note-result",
                                path="notes/result.md",
                                title="Итог наброска",
                                body="Синтетический текст заметки.",
                            ),
                        ),
                    )
                    return [HarnessEvent("result", reply.text)]
                await plan["release"].wait()
                if plan["mode"] == "fail":
                    raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
                return [HarnessEvent("result", reply.text)]  # late: after the lease was lost

            async def close(self, key):
                pass

            async def cancel(self, key):
                pass

        async def factory(execution, gateway, gate):
            return Harness(execution, gateway)

        rt.session_harness = factory

    monkeypatch.setattr(support, "prepared", prepared)
    monkeypatch.setattr(support, "install", scripted_install)
    return plan


@pytest.fixture
def progress_host(scripted, work_host):
    return work_host, scripted


def release(plan):
    plan["loop"].call_soon_threadsafe(plan["release"].set)


def open_conversation(page, origin, host, title, text):
    page.goto(origin + "/#/work")
    page.get_by_label("Пароль", exact=True).fill(paths(host.root).password.read_text().strip())
    page.get_by_role("button", name="Войти", exact=True).click()
    talk = page.get_by_label("Разговор о работе")
    talk.get_by_label("Новый разговор о работе").fill(title)
    talk.get_by_role("button", name="Начать разговор").click()
    thread = page.get_by_label("Разговор: " + title)
    thread.get_by_label("Сообщение агенту").fill(text)
    thread.get_by_role("button", name="Отправить").click()
    return thread


def turn_of(page, title):
    return page.get_by_label("Разговор: " + title).get_by_label("Ход выполнения")


def test_waiting_running_and_an_explicit_error_without_any_resend(page, progress_host):
    (origin, host, model), plan = progress_host
    plan["mode"] = "fail"
    open_conversation(page, origin, host, "Ошибка хода", "Синтетический вопрос.")
    turn = turn_of(page, "Ошибка хода")
    expect(turn).to_contain_text("ждёт запуска агента", timeout=10000)
    expect(turn).to_contain_text("Нажмите START")
    assert model.count("activity") == 0

    model.queues["activity"].append({"reply": REPLY})
    page.get_by_role("button", name="START — запустить агента").click()
    # Running: the confirmed model call is reported from the LLM journal, not a timer.
    expect(turn).to_contain_text("Агент выполняет рабочий ход", timeout=30000)
    expect(turn).to_contain_text("Вызовы модели — завершено: 1", timeout=15000)
    release(plan)
    expect(turn).to_contain_text("Рабочий ход завершился ошибкой", timeout=30000)
    expect(turn).to_contain_text("Повторно ничего само не отправится")
    thread = page.get_by_label("Разговор: Ошибка хода")
    expect(thread.get_by_text(re.compile(r"· ответа нет$"))).to_be_visible()
    expect(thread.get_by_text("Чей ход: Рабочий ход завершился ошибкой")).to_be_visible()
    turn.get_by_text("Подробности").click()
    expect(turn).to_contain_text("Код: CAPABILITY_UNAVAILABLE")
    calls = model.count("activity")

    page.reload()
    turn = turn_of(page, "Ошибка хода")
    expect(turn).to_contain_text("Рабочий ход завершился ошибкой", timeout=15000)
    expect(turn).to_contain_text("ничего не отправляет повторно")
    page.wait_for_timeout(1500)
    assert model.count("activity") == calls  # reload and reconnect resend nothing


def test_an_unconfirmed_outcome_is_shown_as_unknown(page, progress_host, monkeypatch):
    from ai_agent_core.m3_composition import service

    (origin, host, model), plan = progress_host
    plan["mode"] = "unknown"

    async def frozen(self):  # the worker stops renewing its lease (it hangs)
        await asyncio.Event().wait()

    monkeypatch.setattr(service.Execution, "renew", frozen)
    open_conversation(page, origin, host, "Неизвестный исход", "Синтетический вопрос.")
    model.queues["activity"].append({"reply": REPLY})
    page.get_by_role("button", name="START — запустить агента").click()
    turn = turn_of(page, "Неизвестный исход")
    expect(turn).to_contain_text("Агент выполняет рабочий ход", timeout=30000)

    async def job_status():
        from ai_agent_contracts.agent_workplace import WorkAttempt

        from ai_agent_core.m3_composition.activity import book

        rt = plan["rt"]
        [attempt] = [a for a in await book(rt).attempts_repo(host.scope).list() if a.session_id]
        loaded = await rt.jobs.store.load(attempt.operation_id, attempt.scope)
        assert isinstance(attempt, WorkAttempt)
        return loaded[0].status

    # The job owner's own reconciliation closes the lost lease as OUTCOME_UNKNOWN.
    for _ in range(80):
        if (
            asyncio.run_coroutine_threadsafe(job_status(), plan["loop"]).result(10)
            == OperationState.OUTCOME_UNKNOWN
        ):
            break
        page.wait_for_timeout(500)
    expect(turn).to_contain_text("Исход рабочего хода не подтверждён", timeout=20000)
    expect(turn).to_contain_text("не повторяет такой ход вслепую")
    page.reload()  # an unconfirmed outcome stays unconfirmed; nothing is resent
    turn = turn_of(page, "Неизвестный исход")
    expect(turn).to_contain_text("Исход рабочего хода не подтверждён", timeout=15000)
    # When the hung worker finally reports, the owner reconciles the fact: the late result is
    # not applied and the turn is told as cancelled, not as a reply.
    release(plan)
    expect(turn).to_contain_text("Рабочий ход отменён", timeout=20000)
    thread = page.get_by_label("Разговор: Неизвестный исход")
    expect(thread.get_by_role("list", name="Сообщения разговора").get_by_text(REPLY)).to_have_count(
        0
    )
    assert model.count("activity") == 1


def test_a_saved_file_is_linked_and_the_reply_is_delivered_once(page, progress_host):
    (origin, host, model), plan = progress_host
    plan["mode"] = "file"
    open_conversation(page, origin, host, "Файл хода", "Сохрани итог.")
    model.queues["activity"].append({"reply": REPLY})
    page.get_by_role("button", name="START — запустить агента").click()
    thread = page.get_by_label("Разговор: Файл хода")
    messages = thread.get_by_role("list", name="Сообщения разговора")
    expect(messages.get_by_text(REPLY)).to_be_visible(timeout=30000)
    turn = turn_of(page, "Файл хода")
    expect(turn).to_contain_text("Рабочий ход завершён", timeout=15000)
    saved = turn.get_by_label("Сохранённые файлы")
    expect(saved.get_by_role("link", name="Итог наброска")).to_have_attribute("href", "#/library")
    expect(saved).to_contain_text("notes/result.md")
    page.reload()
    thread = page.get_by_label("Разговор: Файл хода")
    expect(thread.get_by_role("list", name="Сообщения разговора").get_by_text(REPLY)).to_have_count(
        1, timeout=15000
    )
    assert model.count("activity") == 1


def test_stop_during_a_turn_is_told_as_cancelled_and_no_reply_appears(page, progress_host):
    (origin, host, model), plan = progress_host
    plan["mode"] = "late"
    open_conversation(page, origin, host, "STOP хода", "Синтетический вопрос.")
    model.queues["activity"].append({"reply": REPLY})
    page.get_by_role("button", name="START — запустить агента").click()
    turn = turn_of(page, "STOP хода")
    expect(turn).to_contain_text("Агент выполняет рабочий ход", timeout=30000)
    # STOP during the turn: only once its model call is made (the job may still be starting).
    asyncio.run_coroutine_threadsafe(plan["entered"].wait(), plan["loop"]).result(30)
    page.get_by_role("button", name="STOP — остановить активного агента").click()
    release(plan)  # the result arrives after STOP: it is not applied
    expect(turn).to_contain_text("Рабочий ход отменён", timeout=30000)
    expect(turn).to_contain_text("запустите агента")
    page.reload()
    turn = turn_of(page, "STOP хода")
    expect(turn).to_contain_text("Рабочий ход отменён", timeout=15000)
    thread = page.get_by_label("Разговор: STOP хода")
    expect(thread.get_by_role("list", name="Сообщения разговора").get_by_text(REPLY)).to_have_count(
        0
    )
    expect(page.get_by_label("Ответ агента, ожидающий получения")).to_have_count(0)
    assert model.count("activity") == 1
