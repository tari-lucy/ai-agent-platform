"""M4-01 acceptance: real access paths separately from deterministic UI fixtures."""

import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect

WORK = {
    "workspace_id": "synthetic",
    "persona_id": "alpha",
    "memory_space_id": "alpha-work",
    "memory_mode": "work",
}
TEST = {**WORK, "persona_id": "beta", "memory_space_id": "beta-test", "memory_mode": "test"}
NOW = "2026-10-14T00:00:00Z"


def run_record(scope, state="ACTIVE"):
    return {
        "id": "run-" + scope["persona_id"],
        "scope": {
            **scope,
            "agent_id": "primary",
            "run_id": "run-" + scope["persona_id"],
            "session_id": "session-" + scope["persona_id"],
        },
        "agent_id": "primary",
        "run_id": "run-" + scope["persona_id"],
        "session_id": "session-" + scope["persona_id"],
        "created_at": NOW,
        "updated_at": NOW,
        "version": 1,
        "control_epoch": 1,
        "state": state,
        "activity": "OPEN",
        "autonomy": "observation",
        "voice_enabled": False,
        "gm_paused": True,
        "unconfirmed_resource_ids": [],
    }


def mock_panel(page, *, profile_mode="unavailable", stop_mode="waiting"):
    calls, sockets = [], []
    state = {"logged_in": True, "run": "ACTIVE"}

    def route(request):
        path = urlsplit(request.request.url).path
        body = request.request.post_data_json if request.request.method == "POST" else None
        calls.append((path, body))
        if path == "/api/v1/auth/session":
            value = {
                "principal_id": "operator",
                "scopes": [WORK, TEST],
                "expires_at": "2099-01-01T00:00:00Z",
                "csrf_token": "s" * 43,
            }
            request.fulfill(
                status=200 if state["logged_in"] else 401, json=value if state["logged_in"] else {}
            )
        elif path == "/api/v1/sessions":
            selected = parse_qs(urlsplit(request.request.url).query)["persona_id"][0]
            scope = WORK if selected == "alpha" else TEST
            row = run_record(scope, state["run"]) if selected == "alpha" else None
            request.fulfill(
                json={
                    "items": [row] if row else [],
                    "limit": 50,
                    "next_cursor": None,
                    "complete": True,
                }
            )
        elif path == "/api/v1/personas":
            if profile_mode == "unavailable":
                request.fulfill(
                    status=503, json={"code": "SERVICE_UNAVAILABLE", "message": "private-details"}
                )
            else:
                selected = parse_qs(urlsplit(request.request.url).query)["persona_id"][0]
                scope = WORK if selected == "alpha" else TEST
                text = "<script>window.fakeExecuted = true</script>"
                row = {
                    "id": "profile-1",
                    "scope": scope,
                    "persona_id": scope["persona_id"],
                    "canon": text,
                    "style": "Synthetic style",
                    "accepted_revision_id": "rev-1",
                    "initial_values": {},
                    "property_definitions": {},
                    "development_bounds": {},
                    "version": 1,
                }
                if profile_mode == "foreign":
                    row["scope"] = TEST
                request.fulfill(
                    json={"items": [row], "limit": 50, "next_cursor": None, "complete": True}
                )
        elif path == "/api/v1/agents/primary/stop":
            if stop_mode == "lost":
                request.abort()
            else:
                state["run"] = "STOPPING"
                request.fulfill(
                    json={
                        "request_id": body["request_id"],
                        "scope": body["scope"],
                        "run_id": "run-alpha",
                        "control_epoch": 2,
                        "state": "STOPPING",
                        "status": "waiting",
                        "unconfirmed_resource_ids": ["synthetic-worker"],
                    }
                )
        elif path == "/api/v1/auth/logout":
            state["logged_in"] = False
            request.fulfill(status=204)
        else:
            request.fulfill(status=404)

    def socket(route):
        sockets.append(route)
        route.on_message(lambda _: None)

    page.route("**/api/v1/**", route)
    page.route_web_socket("**/api/v1/events", socket)
    return calls, sockets, state


def open_mock(page, origin, **kwargs):
    state = mock_panel(page, **kwargs)
    page.goto(origin)
    expect(page.get_by_role("button", name="STOP — остановить активного агента")).to_be_enabled()
    return state


def test_real_login_cookie_reload_stop_ws_logout(page, live_panel, panel_password):
    page.goto(live_panel)
    expect(page.get_by_role("heading", name="Вход оператора")).to_be_visible()
    page.get_by_label("Пароль", exact=True).fill(panel_password)
    page.get_by_role("button", name="Войти", exact=True).click()
    stop = page.get_by_role("button", name="STOP — остановить активного агента")
    expect(stop).to_be_enabled()
    expect(page.locator(".header-status")).to_contain_text("События подключены")
    cookie = next(c for c in page.context.cookies() if c["name"] == "ai_operator_session")
    assert cookie["httpOnly"] and cookie["sameSite"] == "Strict"
    assert not page.evaluate("localStorage.length + sessionStorage.length")
    assert panel_password not in page.content()
    page.reload()
    expect(stop).to_be_enabled()
    stop.click()
    expect(page.get_by_text("Ядро подтвердило остановку.", exact=False)).to_be_visible()
    page.get_by_role("link", name="Рабочее пространство", exact=True).click()
    expect(page.get_by_text("Тестовая память", exact=True)).to_be_visible()
    page.get_by_role("button", name="Выйти", exact=True).click()
    expect(page.get_by_role("heading", name="Вход оператора")).to_be_visible()
    assert not any(c["name"] == "ai_operator_session" for c in page.context.cookies())
    page.reload()
    expect(page.get_by_role("heading", name="Вход оператора")).to_be_visible()


def test_wrong_password_is_safe_and_cleared(page, live_panel):
    page.goto(live_panel)
    page.get_by_label("Пароль", exact=True).fill("synthetic-wrong-password")
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Доступ запрещён")
    expect(page.get_by_label("Пароль", exact=True)).to_have_value("")
    assert "synthetic-wrong-password" not in page.content()


def test_selected_test_profile_does_not_retarget_stop(page, live_panel):
    calls, _, _ = open_mock(page, live_panel)
    page.get_by_role("combobox", name="Просматриваемый персонаж и память", exact=True).click()
    page.get_by_role("option", name="beta · ТЕСТ · beta-test", exact=True).click()
    expect(page.locator(".active-run")).to_contain_text("alpha")
    expect(page.get_by_text("Тестовая память", exact=True)).to_be_visible()
    page.get_by_role("button", name="STOP — остановить активного агента").click()
    expect(page.get_by_text("Допуски отозваны.", exact=False)).to_be_visible()
    body = next(body for path, body in calls if path.endswith("/primary/stop"))
    assert body["scope"] == {
        **WORK,
        "agent_id": "primary",
        "run_id": "run-alpha",
        "session_id": "session-alpha",
    }
    assert body["target"] == "primary"
    assert "synthetic-worker" in page.content()
    assert "Ядро подтвердило остановку." not in page.content()


def test_stop_available_on_all_seven_screens_and_mobile(page, live_panel):
    open_mock(page, live_panel)
    nav = page.get_by_role("navigation", name="Разделы панели")
    for label in [
        "Диалог",
        "Персонаж",
        "Память и люди",
        "Сюжет и развитие",
        "Лаборатория",
        "Рабочее пространство",
        "LLM и расходы",
    ]:
        nav.get_by_role("link", name=label, exact=True).click()
        expect(
            page.get_by_role("button", name="STOP — остановить активного агента")
        ).to_be_in_viewport()
        expect(page.get_by_role("heading", level=1)).to_be_visible()
    page.screenshot(
        path=str(Path(__file__).resolve().parents[2] / "runtime/panel-desktop.png"),
        full_page=True,
    )
    page.set_viewport_size({"width": 390, "height": 844})
    stop = page.get_by_role("button", name="STOP — остановить активного агента")
    expect(stop).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("combobox", name="Просматриваемый персонаж и память").focus()
    page.keyboard.press("Tab")
    expect(stop).to_be_focused()
    output = page.locator("main").bounding_box()
    header = page.locator("header").bounding_box()
    assert output["y"] >= header["height"]
    page.screenshot(
        path=str(Path(__file__).resolve().parents[2] / "runtime/panel-mobile.png"),
        full_page=True,
    )


def test_ws_reconnect_refreshes_get_without_replaying_commands(page, live_panel):
    calls, sockets, _ = open_mock(page, live_panel)
    before = sum(path == "/api/v1/sessions" for path, _ in calls)
    sockets[0].close(code=1013)
    expect(page.locator(".header-status")).to_contain_text("События подключены")
    deadline = time.monotonic() + 5
    while len(sockets) < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(sockets) >= 2
    assert sum(path == "/api/v1/sessions" for path, _ in calls) > before
    assert not any(body is not None for _, body in calls)


def test_foreign_projection_hidden_and_markup_is_plain_text(page, live_panel):
    open_mock(page, live_panel, profile_mode="foreign")
    page.get_by_role("link", name="Персонаж", exact=True).click()
    expect(page.get_by_text("Не удалось получить данные.", exact=False)).to_be_visible()
    assert "<script>" not in page.locator("main").inner_text()


def test_plain_profile_does_not_execute_html(page, live_panel):
    open_mock(page, live_panel, profile_mode="text")
    page.get_by_role("link", name="Персонаж", exact=True).click()
    # The canon is shown twice now (accepted profile and editor field); the first is the plain text.
    expect(
        page.get_by_label("Принятый профиль").get_by_text(
            "<script>window.fakeExecuted = true</script>", exact=True
        )
    ).to_be_visible()
    assert page.evaluate("window.fakeExecuted") is None


def test_timeout_is_unknown_and_never_retries_stop(page, live_panel):
    calls, _, _ = open_mock(page, live_panel, stop_mode="lost")
    page.get_by_role("button", name="STOP — остановить активного агента").click()
    expect(page.get_by_text("Результат STOP неизвестен", exact=True)).to_be_visible()
    assert sum(path.endswith("/primary/stop") for path, _ in calls) == 1


def test_expired_session_hides_previous_data(page, live_panel):
    _, sockets, state = open_mock(page, live_panel)
    state["logged_in"] = False
    sockets[0].close(code=1008)
    expect(page.get_by_role("heading", name="Вход оператора")).to_be_visible()
    assert not page.evaluate("localStorage.length + sessionStorage.length")
    assert page.get_by_role("navigation", name="Разделы панели").count() == 0
    assert "run-alpha" not in page.content()


def test_real_foreign_origin_and_missing_csrf_are_denied(page, live_panel, panel_password):
    # API security remains server-owned; the UI sends a browser-originated login.
    page.goto(live_panel)
    page.get_by_label("Пароль", exact=True).fill(panel_password)
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.get_by_role("button", name="STOP — остановить активного агента")).to_be_enabled()
    status = page.evaluate("""async () => (await fetch('/api/v1/agents/primary/stop', {
        method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({type:'agent.stop', request_id:'no-csrf',target:'primary',
          scope:{workspace_id:'synthetic',persona_id:'alpha',memory_space_id:'test-bootstrap',memory_mode:'test'}})
      })).status""")
    assert status == 403
    status = page.context.request.get(
        live_panel + "/api/v1/auth/session", headers={"Host": "foreign.invalid"}
    ).status
    assert status == 403
    response = page.context.request.post(
        live_panel + "/api/v1/auth/login",
        headers={"Origin": "https://foreign.invalid"},
        data=json.dumps({"password": panel_password}),
    )
    assert response.status == 403


def test_real_stop_while_profile_read_is_pending(page, live_panel, panel_password):
    page.goto(live_panel)
    page.get_by_label("Пароль", exact=True).fill(panel_password)
    page.get_by_role("button", name="Войти", exact=True).click()
    stop = page.get_by_role("button", name="STOP — остановить активного агента")
    expect(stop).to_be_enabled()
    pending = []
    page.route("**/api/v1/personas?*", lambda route: pending.append(route))
    page.get_by_role("link", name="Персонаж", exact=True).click()
    expect(page.get_by_text("Получаем профиль…", exact=True)).to_be_visible()
    stop.click()
    expect(page.get_by_text("Ядро подтвердило остановку.", exact=False)).to_be_visible()
    assert pending
    for route in pending:
        route.abort()
