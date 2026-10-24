"""Fresh/expired native login uses the panel's clean URL, no forbidden query string."""

import os

import pytest
from playwright.sync_api import expect
from test_native_opencode_sessions import native_distribution as native_distribution
from test_work_conversation_ack_browser import work_host as work_host

from ai_agent_core.local_host.layout import paths

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_NATIVE_UI_DIR") or os.getenv("AW_OPENCODE_IMAGE_ID"),
    reason="packaged native UI and synthetic harness required",
)


@pytest.fixture(autouse=True)
def native(native_distribution):
    return native_distribution


def test_native_fresh_and_expired_login_return_to_clean_panel(page, work_host):
    origin, host, model = work_host
    page.goto(origin + "/opencode/")
    expect(page.get_by_label("Пароль", exact=True)).to_be_visible(timeout=20000)
    expect(page).to_have_url(origin + "/")
    page.get_by_label("Пароль", exact=True).fill(paths(host.root).password.read_text().strip())
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.get_by_role("button", name="START — запустить агента")).to_be_visible()
    link = page.get_by_role("link", name="Открыть OpenCode")
    expect(link).to_be_visible(timeout=15000)
    link.click()
    expect(page.locator('[data-component="prompt-input"]')).to_be_visible(timeout=20000)

    page.context.clear_cookies()
    page.goto(origin + "/opencode/")
    expect(page.get_by_label("Пароль", exact=True)).to_be_visible(timeout=20000)
    expect(page).to_have_url(origin + "/")
    assert page.request.get(origin + "/?next=%2Fopencode%2F").status == 400
    assert model.count("activity") == 0
