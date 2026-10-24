"""OC-06 (Linux/headless part): native OpenCode window around a ticket, a revocation and the
boundaries of the native surface. Production host owners, synthetic model, packaged native UI.

Not repeated here (see test_native_opencode_sessions.py): reload STOPPED/ACTIVE, hidden or
clipped reply without ACK, exact visible ACK, no duplicate after reload, STOP.

Checked: the person asks the operator in a ticket during a conversation; the native window
says it waits and links the exact ticket card in the panel; the operator answers there in plain
text; the same conversation continues in the native window without a new operator message.
After STOP a new message whose basis is revoked is never executed and the native window says
so. Native OpenCode APIs/PTY are not served, a foreign directory is refused, and the page
talks to the platform only through its own assets and the bridge.
"""

import asyncio
import base64
import json
import os
import time
from urllib.parse import urlsplit

import pytest
import test_work_conversation_ack_browser as support
from ai_agent_application.agent_workplace.ports import WorkspaceRequest
from playwright.sync_api import expect
from test_native_opencode_sessions import native_distribution as native_distribution
from test_work_conversation_ack_browser import work_host as work_host

from ai_agent_core.local_host.layout import paths
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.state import command as owner_command
from ai_agent_core.m3_composition.work_session_workspace import Workspace
from ai_agent_core.m3_composition.work_sessions import sessions
from ai_agent_core.opencode_ui.protocol import PREFIX, directory

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_NATIVE_UI_DIR") or os.getenv("AW_OPENCODE_IMAGE_ID"),
    reason="packaged native OpenCode build with the synthetic harness required",
)
QUESTION = "Сделай синтетический набросок для газеты."
CLARIFY = "Уточню формат у оператора в обращении."
ANSWER = "Горизонтальный, на полосу."
CONTINUED = "Сделаю горизонтальный набросок на полосу."
LATE = "Синтетическое сообщение, основание которого будет отозвано."


@pytest.fixture(autouse=True)
def workplace_files(monkeypatch, tmp_path, native_distribution):
    # The session's ticket tool needs the workplace owners (recovery ledger + managed files).
    prepare = support.prepared

    async def prepared(directory, patch, model):
        host = await prepare(directory, patch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await enable_workplace_files(host.root)
        return host

    monkeypatch.setattr(support, "prepared", prepared)


def test_native_ticket_round_trip_revocation_and_closed_native_surface(page, work_host):
    origin, host, model = work_host
    rt, loop = host.served
    seen: list[str] = []
    page.on("request", lambda request: seen.append(urlsplit(request.url).path))

    # The first turn of this conversation asks the operator by ticket (as OpenCode would).
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
    selected = directory(host.scope)
    bridge = origin + PREFIX + "/agents/" + selected
    opened = page.request.post(
        bridge + "/commands",
        data=dict(
            type="work.session.open",
            request_id="journey-open",
            target="work",
            scope=host.scope.model_dump(mode="json"),
            arguments=dict(schema_id="work.session.open-args", data={"title": "Набросок"}),
        ),
        headers={"Origin": origin, "x-csrf-token": csrf},
    ).json()["resource_id"]
    slug = base64.urlsafe_b64encode(selected.encode()).decode().rstrip("=")
    page.goto(origin + "/opencode/" + slug + "/session/" + opened)

    def send(text):
        prompt = page.locator('[data-component="prompt-input"][contenteditable="true"]')
        prompt.fill(text)
        expect(page.locator('[data-action="prompt-submit"]')).to_be_enabled(timeout=20000)
        # The adapter may retry once after a version conflict; wait for the accepted command.
        with page.expect_response(
            lambda r: r.url == bridge + "/commands" and r.request.method == "POST" and r.ok,
            timeout=20000,
        ):
            page.locator('[data-action="prompt-submit"]').click()
        texts = [e["text"] for e in owner_view()["entries"] if e["speaker"] == "operator"]
        assert texts.count(text) == 1, texts  # stored exactly once

    def owner_view():
        return page.request.get(bridge + "/sessions/" + opened).json()

    def delivered(count):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            view = owner_view()
            if view["pending_reply"] is None and len(view["entries"]) == count:
                return view
            time.sleep(0.2)
        raise AssertionError(owner_view())

    send(QUESTION)
    expect(page.get_by_text("ждёт, пока эта личность будет запущена", exact=False)).to_be_visible(timeout=20000)

    model.queues["activity"].append({"reply": CLARIFY})
    control = page.context.new_page()
    control.goto(origin + "/")
    control.get_by_role("button", name="START — запустить агента").click()
    expect(control.locator(".active-run")).to_contain_text("Работает", timeout=20000)
    # Edge backgrounds the native tab when the operator opens the control panel.
    # ACK requires the operator to return to the conversation, not just DOM visibility.
    page.bring_to_front()
    assert page.evaluate("document.visibilityState") == "visible"
    expect(page.get_by_text(CLARIFY, exact=True).first).to_be_visible(timeout=60000)
    delivered(2)  # the visible reply was acknowledged once
    assert len(asked) == 1  # the person asked the operator in a ticket during the turn
    # The conversation now waits for the operator on exactly that ticket; the native window
    # says so and points to the exact card in the panel (it does not answer itself).
    assert owner_view()["open_ticket_ids"] == asked
    page.reload()
    expect(page.get_by_text("Агент ждёт вашего ответа в обращении", exact=False)).to_be_visible(
        timeout=30000
    )
    expect(page.get_by_text("/#/tickets?ticket=" + asked[0], exact=False)).to_be_visible()

    # The operator answers in the panel in plain text; the same conversation continues.
    model.queues["activity"].append({"reply": CONTINUED})
    control.goto(origin + "/#/tickets?ticket=" + asked[0])  # the exact card, no list search
    card = control.get_by_label("Карточка обращения")
    expect(card.get_by_text("Какой формат наброска?")).to_be_visible(timeout=15000)
    card.get_by_label("Ваш ответ").fill(ANSWER)
    card.get_by_role("button", name="Отправить ответ").click()
    expect(card.get_by_role("status")).to_contain_text("Ответ отправлен", timeout=15000)
    page.bring_to_front()
    assert page.evaluate("document.visibilityState") == "visible"
    expect(page.get_by_text(CONTINUED, exact=True).first).to_be_visible(timeout=60000)
    view = delivered(3)
    assert view["open_ticket_ids"] == []  # answered: nothing waits for the operator now
    assert [e["speaker"] for e in view["entries"]] == ["operator", "character", "character"]
    assert model.count("activity") == 2
    assert ANSWER in json.dumps(model.last("activity"), ensure_ascii=False)

    # STOP, then a message whose basis is revoked: never executed, the window says so.
    control.goto(origin + "/")
    control.get_by_role("button", name="STOP — остановить активного агента").click()
    expect(control.get_by_role("button", name="START — запустить агента")).to_be_visible()
    send(LATE)

    async def revoke():
        rows = await sessions(rt).messages(host.scope, opened)
        source = rows[-1].source
        await memory(rt).set_rights(
            owner_command(host.scope, "journey-revoke"),
            source.source_id,
            source.version,
            read_allowed=True,
            api_transfer_allowed=False,
            public_disclosure_allowed=False,
        )

    asyncio.run_coroutine_threadsafe(revoke(), loop).result(20)
    control.get_by_role("button", name="START — запустить агента").click()
    page.reload()
    expect(page.get_by_text("Разговор остановлен", exact=False)).to_be_visible(timeout=30000)
    time.sleep(2)
    assert model.count("activity") == 2  # the revoked message ran nothing
    control.close()

    # The native surface is closed: no native API/PTY, no foreign directory.
    for path in ("/opencode/api/session", "/opencode/pty", "/opencode/global/health"):
        assert page.request.get(origin + path).status in (403, 404), path
    foreign = origin + PREFIX + "/agents/aw_" + "0" * 64 + "/sessions"
    assert page.request.get(foreign).status in (403, 404)
    # The page reached only its assets, the panel and the platform API (no native routes).
    allowed = ("/opencode/", "/api/v1/", "/assets/", "/favicon")
    assert all(p == "/" or p.startswith(allowed) for p in seen), sorted(
        {p for p in seen if p != "/" and not p.startswith(allowed)}
    )
    assert page.evaluate("() => [localStorage.length, sessionStorage.length]") == [0, 0]
