"""PUI-03 (Linux/headless): with the native OpenCode window served, the panel sends ordinary
conversation and new work there and keeps old conversations for reading only.

Production host owners, packaged native UI, synthetic data; no agent is started and no
model is called. Checked: the header links the selected person in OpenCode; "Диалог" becomes
diagnostic reading without a composer or briefing form; the work screen has no assignment form,
no "discuss" button and no conversation editor, only a link to the exact native conversation,
which opens it. Without the native window the old forms stay (covered by the existing
dialogue/work browser tests, unchanged). History, Source and Job are not touched.
"""

import base64
import os

import pytest
from playwright.sync_api import expect
from test_native_opencode_sessions import native_distribution as native_distribution
from test_work_conversation_ack_browser import work_host as work_host

from ai_agent_core.local_host.layout import paths
from ai_agent_core.opencode_ui.protocol import PREFIX, directory

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_NATIVE_UI_DIR") or os.getenv("AW_OPENCODE_IMAGE_ID"),
    reason="packaged native OpenCode build with the synthetic harness required",
)


@pytest.fixture(autouse=True)
def native(native_distribution):
    return native_distribution


def test_panel_points_conversation_and_work_to_opencode_and_keeps_reading(page, work_host):
    origin, host, model = work_host
    page.goto(origin + "/")
    page.get_by_label("Пароль", exact=True).fill(paths(host.root).password.read_text().strip())
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.get_by_role("button", name="START — запустить агента")).to_be_visible()
    selected = directory(host.scope)
    slug = base64.urlsafe_b64encode(selected.encode()).decode().rstrip("=")

    # The header links the viewed person in the native window.
    link = page.get_by_role("link", name="Открыть OpenCode")
    expect(link).to_have_attribute("href", "/opencode/" + slug + "/", timeout=15000)

    # "Диалог" is reading only: no composer and no briefing form.
    nav = page.get_by_role("navigation", name="Разделы панели")
    nav.get_by_role("link", name="Прежний диалог · чтение").click()
    expect(page.get_by_text("Разговор теперь в OpenCode")).to_be_visible()
    expect(page.get_by_label("Сообщение персонажу")).to_have_count(0)
    expect(page.get_by_label("Текст вводной")).to_have_count(0)

    # One conversation exists (opened through the bridge, as OpenCode does).
    csrf = page.request.get(origin + "/api/v1/auth/session").json()["csrf_token"]
    opened = page.request.post(
        origin + PREFIX + "/agents/" + selected + "/commands",
        data=dict(
            type="work.session.open",
            request_id="pui03-open",
            target="work",
            scope=host.scope.model_dump(mode="json"),
            arguments=dict(schema_id="work.session.open-args", data={"title": "Набросок"}),
        ),
        headers={"Origin": origin, "x-csrf-token": csrf},
    ).json()["resource_id"]

    # Work: no assignment form, no discuss button, no editor; one link to the exact conversation.
    nav.get_by_role("link", name="Работа агента").click()
    expect(page.get_by_text("Поручения и разговор — в OpenCode")).to_be_visible(timeout=15000)
    expect(page.get_by_label("Новое поручение")).to_have_count(0)
    expect(page.get_by_role("button", name="Обсудить с агентом")).to_have_count(0)
    expect(page.get_by_label("Сообщение агенту")).to_have_count(0)
    expect(page.get_by_role("button", name="Начать разговор")).to_have_count(0)
    thread = page.get_by_label("Разговор: Набросок")
    native_link = thread.get_by_role("link", name="Открыть в OpenCode")
    expect(native_link).to_have_attribute("href", "/opencode/" + slug + "/session/" + opened)
    native_link.click()
    expect(page).to_have_url(origin + "/opencode/" + slug + "/session/" + opened, timeout=30000)
    expect(page.locator('[data-component="prompt-input"]')).to_be_visible(timeout=30000)

    assert model.count("activity") == 0

    # If readiness cannot be confirmed, the panel keeps its legacy input available.
    # This is a fresh panel load, not a native API fallback or an automatic START.
    page.route("**/opencode/", lambda route: route.abort())
    page.goto(origin + "/#/dialogue")
    expect(page.get_by_label("Сообщение персонажу")).to_be_visible(timeout=15000)
    expect(page.get_by_role("link", name="Открыть OpenCode")).to_have_count(0)
    assert model.count("activity") == 0
