"""OC-02 (Linux/headless): two fictional persons in the native OpenCode window.

A temporary host gets a second, inactive person B through the ordinary offline `add-persona`
before serve (no scope substitution, nothing of A copied). The native window lists both;
B accepts a message that waits (B never runs: only A is the active executor); A answers with
its own memory only — B's marker never reaches A's model request. The selected conversation
survives a reload, a session of B cannot be read through A's directory, and the browser keeps
nothing. Synthetic model and data, production owners, packaged native UI.
"""

import base64
import json
import os

import pytest
import test_work_conversation_ack_browser as support
from ai_agent_application.laboratory.profile import REFERENCE_PROFILE
from playwright.sync_api import expect
from test_native_opencode_sessions import native_distribution as native_distribution
from test_work_conversation_ack_browser import work_host as work_host

from ai_agent_core.local_host.layout import paths
from ai_agent_core.local_host.personas import add_persona
from ai_agent_core.opencode_ui.protocol import PREFIX

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_NATIVE_UI_DIR") or os.getenv("AW_OPENCODE_IMAGE_ID"),
    reason="packaged native OpenCode build with the synthetic harness required",
)
B = "beacon"
FOR_A = "Синтетический вопрос для личности А: маркер-альфа."
FOR_B = "Синтетический вопрос для личности Б: маркер-бета."
REPLY_A = "Синтетический ответ личности А."


@pytest.fixture(autouse=True)
def second_person(monkeypatch, native_distribution):
    prepare = support.prepared

    async def prepared(directory, patch, model):
        host = await prepare(directory, patch, model)
        await add_persona(host.root, persona_id=B, profile_markdown=REFERENCE_PROFILE)
        return host

    monkeypatch.setattr(support, "prepared", prepared)


def test_two_persons_in_the_native_window_keep_their_own_conversations(page, work_host):
    origin, host, model = work_host
    page.goto(origin + "/")
    page.get_by_label("Пароль", exact=True).fill(paths(host.root).password.read_text().strip())
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.get_by_role("button", name="START — запустить агента")).to_be_visible()
    csrf = page.request.get(origin + "/api/v1/auth/session").json()["csrf_token"]
    catalogue = page.request.get(origin + PREFIX + "/agents").json()["items"]
    work = {item["label"]: item for item in catalogue if item["scope"]["memory_mode"] == "work"}
    assert set(work) == {host.scope.persona_id, B}
    a, b = work[host.scope.persona_id], work[B]
    assert b["active_runtime"] is False

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

    # Both persons are in the native catalogue (their names, not directory keys).
    page.goto(origin + "/opencode/")
    expect(page.get_by_text(B, exact=True).first).to_be_visible(timeout=30000)
    expect(page.get_by_text(host.scope.persona_id, exact=True).first).to_be_visible()

    # B: a message is stored and waits; B is viewed, never activated.
    session_b = open_session(b, "open-b", "Разговор с Б")
    page.goto(url(b, session_b))
    send(FOR_B, b)
    expect(page.get_by_text("ждёт, пока эта личность будет запущена", exact=False)).to_be_visible(
        timeout=20000
    )

    # A: a message, then START — only A, the active executor, answers.
    session_a = open_session(a, "open-a", "Разговор с А")
    page.goto(url(a, session_a))
    send(FOR_A, a)
    model.queues["activity"].append({"reply": REPLY_A})
    control = page.context.new_page()
    control.goto(origin + "/")
    control.get_by_role("button", name="START — запустить агента").click()
    expect(control.locator(".active-run")).to_contain_text("Работает", timeout=20000)
    control.close()
    # The operator returns to the conversation (a background tab never acknowledges).
    page.bring_to_front()
    assert page.evaluate("document.visibilityState") == "visible"
    expect(page.get_by_text(REPLY_A, exact=True).first).to_be_visible(timeout=60000)
    assert model.count("activity") == 1
    request = json.dumps(model.last("activity"), ensure_ascii=False)
    assert "маркер-альфа" in request and "маркер-бета" not in request  # A's own memory only

    # Back to B: still waiting, nothing ran for B, and A's words are not there.
    page.goto(url(b, session_b))
    expect(page.get_by_text(FOR_B, exact=True).first).to_be_visible(timeout=30000)
    expect(page.get_by_text("ждёт, пока эта личность будет запущена", exact=False)).to_be_visible()
    assert page.get_by_text(REPLY_A, exact=True).count() == 0
    held = view(b, session_b)
    assert held["processed_sequence"] == 0 and held["pending_reply"] is None
    assert model.count("activity") == 1

    # Reload keeps the selected person and conversation (the address, not a guess).
    page.reload()
    expect(page.get_by_text(FOR_B, exact=True).first).to_be_visible(timeout=30000)
    assert page.get_by_text(FOR_A, exact=True).count() == 0

    # A conversation of B is not readable through A's directory, and vice versa.
    assert page.request.get(bridge(a) + "/sessions/" + session_b).status in (403, 404)
    assert page.request.get(bridge(b) + "/sessions/" + session_a).status in (403, 404)
    assert page.evaluate("() => [localStorage.length, sessionStorage.length]") == [0, 0]
