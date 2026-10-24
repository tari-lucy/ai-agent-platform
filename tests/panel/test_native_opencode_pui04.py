"""PUI-04 (Linux/headless): one browser path from OpenCode to the panel card and back.

Two fictional persons (B added offline by `add-persona`). In the native window A is asked; during
the turn A asks the operator in a ticket. The window links the exact panel card; following the
link in the same tab opens that card (A's ticket, not B's area); the operator answers there; the
browser goes back to the same A conversation, which continues by itself. One owner ticket, one
answer, one model call per turn; a reload makes no new call. B's conversation stays untouched.
Not repeated (see test_native_opencode_journey.py / _sessions.py / _personas.py): STOP and
revocation, closed native surface, ACK of hidden/clipped replies, B never answering.
Synthetic model and data, production owners, packaged native UI.
"""

import base64
import json
import os
import time

import pytest
import test_work_conversation_ack_browser as support
from ai_agent_application.agent_workplace.ports import WorkspaceRequest
from ai_agent_application.laboratory.profile import REFERENCE_PROFILE
from playwright.sync_api import expect
from test_native_opencode_sessions import native_distribution as native_distribution
from test_work_conversation_ack_browser import work_host as work_host

from ai_agent_core.local_host.layout import paths
from ai_agent_core.local_host.personas import add_persona
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.work_session_workspace import Workspace
from ai_agent_core.opencode_ui.protocol import PREFIX

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_NATIVE_UI_DIR") or os.getenv("AW_OPENCODE_IMAGE_ID"),
    reason="packaged native OpenCode build with the synthetic harness required",
)
B = "beacon"
QUESTION = "Синтетическая просьба к личности А: набросок для газеты."
FOR_B = "Синтетическое сообщение для личности Б."
CLARIFY = "Уточню формат у оператора в обращении."
ANSWER = "Горизонтальный, на полосу."
CONTINUED = "Сделаю горизонтальный набросок на полосу."


@pytest.fixture(autouse=True)
def two_persons_with_tickets(monkeypatch, tmp_path, native_distribution):
    prepare = support.prepared

    async def prepared(directory, patch, model):
        host = await prepare(directory, patch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await enable_workplace_files(host.root)
        await add_persona(host.root, persona_id=B, profile_markdown=REFERENCE_PROFILE)
        return host

    monkeypatch.setattr(support, "prepared", prepared)


def test_opencode_ticket_link_panel_answer_and_back_to_the_same_conversation(page, work_host):
    origin, host, model = work_host
    rt, _ = host.served

    # A's first turn asks the operator by ticket (as OpenCode would through its tool).
    asked: list = []
    plain = rt.session_harness

    async def asking(ex, gateway, gate):
        harness = await plain(ex, gateway, gate)
        turn = harness.turn

        async def first(admission, instruction):
            if not asked:
                receipt = json.loads(
                    await Workspace(ex, gate).call(
                        gate,
                        WorkspaceRequest(
                            "ask",
                            "raise_ticket",
                            dict(title="Какой формат наброска?", body="Уточни формат."),
                        ),
                    )
                )
                asked.append(receipt["ticket_id"])
            return await turn(admission, instruction)

        harness.turn = first
        return harness

    rt.session_harness = asking

    page.goto(origin + "/")
    page.get_by_label("Пароль", exact=True).fill(paths(host.root).password.read_text().strip())
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.get_by_role("button", name="START — запустить агента")).to_be_visible()
    csrf = page.request.get(origin + "/api/v1/auth/session").json()["csrf_token"]
    catalogue = page.request.get(origin + PREFIX + "/agents").json()["items"]
    work = {item["label"]: item for item in catalogue if item["scope"]["memory_mode"] == "work"}
    a, b = work[host.scope.persona_id], work[B]

    def bridge(agent):
        return origin + PREFIX + "/agents/" + agent["directory"]

    def open_session(agent, key, title):
        response = page.request.post(
            bridge(agent) + "/commands",
            data=dict(
                type="work.session.open",
                request_id=key,
                target="work",
                scope=agent["scope"],
                arguments=dict(schema_id="work.session.open-args", data={"title": title}),
            ),
            headers={"Origin": origin, "x-csrf-token": csrf},
        )
        assert response.status == 200, response.text()
        return response.json()["resource_id"]

    def url(agent, session):
        slug = base64.urlsafe_b64encode(agent["directory"].encode()).decode().rstrip("=")
        return origin + "/opencode/" + slug + "/session/" + session

    def send(text, agent):
        prompt = page.locator('[data-component="prompt-input"][contenteditable="true"]')
        prompt.fill(text)
        expect(page.locator('[data-action="prompt-submit"]')).to_be_enabled(timeout=20000)
        with page.expect_response(
            lambda r: r.url == bridge(agent) + "/commands" and r.request.method == "POST" and r.ok,
            timeout=20000,
        ):
            page.locator('[data-action="prompt-submit"]').click()

    def view(agent, session):
        return page.request.get(bridge(agent) + "/sessions/" + session).json()

    def delivered(agent, session, count):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            current = view(agent, session)
            if current["pending_reply"] is None and len(current["entries"]) == count:
                return current
            time.sleep(0.2)
        raise AssertionError(view(agent, session))

    def tickets(agent):
        scope = agent["scope"]
        params = {k: scope[k] for k in ("workspace_id", "persona_id", "memory_space_id")}
        params["memory_mode"] = scope["memory_mode"]
        response = page.request.get(origin + "/api/v1/tickets", params=params)
        assert response.ok, response.text()
        return response.json()["items"]

    # B has a waiting message of its own; it must stay untouched by A's path.
    session_b = open_session(b, "open-b", "Разговор с Б")
    page.goto(url(b, session_b))
    send(FOR_B, b)

    # A: message, START, the reply arrives and A has asked the operator in a ticket.
    session_a = open_session(a, "open-a", "Разговор с А")
    conversation = url(a, session_a)
    page.goto(conversation)
    send(QUESTION, a)
    model.queues["activity"].append({"reply": CLARIFY})
    control = page.context.new_page()
    control.goto(origin + "/")
    control.get_by_role("button", name="START — запустить агента").click()
    expect(control.locator(".active-run")).to_contain_text("Работает", timeout=20000)
    control.close()
    page.bring_to_front()
    expect(page.get_by_text(CLARIFY, exact=True).first).to_be_visible(timeout=60000)
    delivered(a, session_a, 2)
    assert len(asked) == 1 and view(a, session_a)["open_ticket_ids"] == asked

    # The window says it waits and gives the exact card address; follow it in the same tab.
    page.reload()
    expect(page.get_by_text("Агент ждёт вашего ответа в обращении", exact=False)).to_be_visible(
        timeout=30000
    )
    card_url = origin + "/#/tickets?ticket=" + asked[0]
    expect(page.get_by_text(card_url, exact=False)).to_be_visible()
    calls = model.count("activity")
    page.goto(card_url)
    card = page.get_by_label("Карточка обращения")
    expect(card.get_by_text("Какой формат наброска?")).to_be_visible(timeout=15000)
    # The address names A's ticket only: B's area has no such ticket.
    assert [t["id"] for t in tickets(a)] == asked
    assert asked[0] not in [t["id"] for t in tickets(b)]

    model.queues["activity"].append({"reply": CONTINUED})
    card.get_by_label("Ваш ответ").fill(ANSWER)
    card.get_by_role("button", name="Отправить ответ").click()
    expect(card.get_by_role("status")).to_contain_text("Ответ отправлен", timeout=15000)

    # Back to the same A conversation: it continues by itself, once.
    page.go_back()
    expect(page).to_have_url(conversation, timeout=30000)
    expect(page.get_by_text(CONTINUED, exact=True).first).to_be_visible(timeout=60000)
    done = delivered(a, session_a, 3)
    assert [e["speaker"] for e in done["entries"]] == ["operator", "character", "character"]
    assert done["open_ticket_ids"] == []
    assert model.count("activity") == calls + 1
    assert ANSWER in json.dumps(model.last("activity"), ensure_ascii=False)

    # One owner ticket with one answer; a reload makes no new model call or reply.
    (ticket,) = tickets(a)
    assert ticket["id"] == asked[0] and ticket["state"] != "waiting_reply"
    page.reload()
    expect(page.get_by_text(CONTINUED, exact=True).first).to_be_visible(timeout=30000)
    time.sleep(2)
    assert model.count("activity") == calls + 1
    assert len(view(a, session_a)["entries"]) == 3

    # B's conversation was not touched: its message still waits, no ticket, no A words.
    held = view(b, session_b)
    assert held["processed_sequence"] == 0 and held["open_ticket_ids"] == []
    assert QUESTION not in json.dumps(held, ensure_ascii=False)
    assert page.evaluate("() => [localStorage.length, sessionStorage.length]") == [0, 0]
