"""Native reply persistence, rename, archive and both operator deletion choices."""

import base64
import os
import time

import pytest
import test_work_conversation_ack_browser as support
from playwright.sync_api import expect
from test_native_opencode_sessions import native_distribution as native_distribution
from test_work_conversation_ack_browser import work_host as work_host

from ai_agent_core.local_host.layout import paths
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.opencode_ui.protocol import PREFIX, directory

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_NATIVE_UI_DIR") or os.getenv("AW_OPENCODE_IMAGE_ID"),
    reason="packaged native UI with synthetic harness required",
)


@pytest.fixture(autouse=True)
def management_host(monkeypatch, native_distribution, tmp_path):
    prepare = support.prepared

    async def prepared(folder, patch, model):
        host = await prepare(folder, patch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await enable_workplace_files(host.root)
        return host

    monkeypatch.setattr(support, "prepared", prepared)


def test_read_answers_stay_and_operator_manages_conversations(page, work_host):
    origin, host, model = work_host
    page.goto(origin + "/")
    page.get_by_label("Пароль", exact=True).fill(paths(host.root).password.read_text().strip())
    page.get_by_role("button", name="Войти", exact=True).click()
    page.get_by_role("button", name="START — запустить агента").wait_for(timeout=20000)
    csrf = page.request.get(origin + "/api/v1/auth/session").json()["csrf_token"]
    selected = directory(host.scope)
    bridge = origin + PREFIX + "/agents/" + selected
    slug = base64.urlsafe_b64encode(selected.encode()).decode().rstrip("=")
    home = origin + "/opencode/" + slug + "/session"
    headers = {"Origin": origin, "x-csrf-token": csrf}

    def open_session(key):
        response = page.request.post(
            bridge + "/commands",
            headers=headers,
            data=dict(
                type="work.session.open",
                request_id=key,
                target="work",
                scope=host.scope.model_dump(mode="json"),
                arguments=dict(schema_id="work.session.open-args", data=dict(title=key)),
            ),
        )
        assert response.status == 200, response.text()
        return response.json()["resource_id"]

    identifier = open_session("management-original")
    url = home + "/" + identifier
    page.goto(url)

    def view():
        response = page.request.get(bridge + "/sessions/" + identifier)
        assert response.status == 200, response.text()
        return response.json()

    def delivered(count):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            current = view()
            if current["pending_reply"] is None and len(current["entries"]) == count:
                return current
            time.sleep(0.15)
        raise AssertionError("synthetic answers did not persist after ACK")

    def send(text):
        prompt = page.locator('[data-component="prompt-input"][contenteditable="true"]')
        prompt.fill(text)
        with page.expect_response(lambda r: r.url == bridge + "/commands" and r.ok, timeout=20000):
            page.locator('[data-action="prompt-submit"]').click()

    first, second = "Первый прочитанный синтетический ответ.", "Второй синтетический ответ."
    model.queues["activity"].append({"reply": first})
    send("Первый синтетический вопрос")
    control = page.context.new_page()
    control.goto(origin + "/")
    control.get_by_role("button", name="START — запустить агента").click()
    expect(control.locator(".active-run")).to_contain_text("Работает", timeout=20000)
    page.bring_to_front()  # A background tab must not acknowledge an unseen reply.
    expect(page.get_by_text(first, exact=True).first).to_be_visible(timeout=30000)
    saved = delivered(2)["entries"][1]
    model.queues["activity"].append({"reply": second})
    send("Второй синтетический вопрос")
    expect(page.get_by_text(second, exact=True).first).to_be_visible(timeout=30000)
    current = delivered(4)
    assert current["entries"][1]["reply_id"] == saved["reply_id"]
    assert current["entries"][1]["at"] == saved["at"]
    page.reload()
    expect(page.get_by_text(first, exact=True).first).to_be_attached(timeout=20000)
    expect(page.get_by_text(second, exact=True).first).to_be_attached(timeout=20000)
    assert model.count("activity") == 2
    control.get_by_role("button", name="STOP — остановить активного агента").click()
    control.get_by_role("button", name="START — запустить агента").wait_for(timeout=20000)
    control.close()

    page.get_by_role("button", name="Переименовать разговор", exact=True).click()
    page.get_by_label("Название разговора", exact=True).fill("Проверка сохранённой переписки")
    page.get_by_role("button", name="Сохранить название", exact=True).click()
    expect(
        page.get_by_role("heading", name="Проверка сохранённой переписки", exact=True)
    ).to_be_visible(timeout=20000)
    page.get_by_role("button", name="В архив", exact=True).click()
    page.wait_for_url(home, timeout=20000, wait_until="domcontentloaded")
    page.get_by_role("button", name="Архив разговоров", exact=True).click()
    expect(
        page.get_by_role("link", name="Проверка сохранённой переписки", exact=True)
    ).to_be_visible()
    page.get_by_role("button", name="Вернуть из архива", exact=True).click()
    page.wait_for_url(url, timeout=20000, wait_until="domcontentloaded")
    expect(page.get_by_text(first, exact=True).first).to_be_attached(timeout=20000)

    # Another conversation can disappear from the list while preserving memory.
    other = open_session("management-keep-memory")
    response = page.request.post(
        bridge + "/commands",
        headers=headers,
        data=dict(
            type="work.session.message",
            request_id="keep-memory-input",
            target=other,
            expected_version=1,
            scope=host.scope.model_dump(mode="json"),
            arguments=dict(
                schema_id="work.session.message-args",
                data=dict(session_id=other, text="Синтетический текст, который нужно сохранить"),
            ),
        ),
    )
    assert response.status == 200
    page.goto(home + "/" + other)
    page.get_by_role("button", name="Удалить разговор…", exact=True).click()
    with page.expect_response(lambda r: r.url == bridge + "/commands"
                              and r.request.method == "POST", timeout=20000) as hidden:
        page.get_by_role("button", name="Убрать из списка, сохранить память", exact=True).click()
    assert hidden.value.status == 200, hidden.value.text()
    try:
        page.wait_for_url(home, timeout=20000, wait_until="domcontentloaded")
    except Exception:
        print("SYNTHETIC_MANAGEMENT_URL", page.url)
        print("SYNTHETIC_MANAGEMENT_ALERT", page.get_by_role("alert").all_text_contents())
        raise
    preserved = page.request.get(bridge + "/sessions/" + other).json()
    assert preserved["entries"][0]["text"] == "Синтетический текст, который нужно сохранить"

    # Full forgetting previews exact dependencies and uses the existing retention owner.
    page.goto(url)
    page.get_by_role("button", name="Удалить разговор…", exact=True).click()
    page.get_by_role("button", name="Заархивировать и посмотреть последствия", exact=True).click()
    page.get_by_role("button", name="Подтверждаю удаление переписки и памяти", exact=True).wait_for(
        timeout=15000
    )
    assert len(view()["entries"]) == 4  # preview did not erase source text
    page.get_by_role("button", name="Подтверждаю удаление переписки и памяти", exact=True).click()
    page.wait_for_url(home, timeout=20000)
    assert view()["entries"] == []
    assert page.request.get(bridge + "/sessions/" + other).json()["entries"][0]["text"]
    assert page.evaluate("() => [localStorage.length, sessionStorage.length]") == [0, 0]
    assert model.count("activity") == 2
