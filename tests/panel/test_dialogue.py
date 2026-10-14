"""M4-02 acceptance: dialogue screen on deterministic projections (UI-only, no live backend)."""

import json
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect

WORK = {
    "workspace_id": "synthetic",
    "persona_id": "alpha",
    "memory_space_id": "alpha-work",
    "memory_mode": "work",
}
TEST = {**WORK, "persona_id": "beta", "memory_space_id": "beta-test", "memory_mode": "test"}
RUN_SCOPE = {**WORK, "agent_id": "primary", "run_id": "run-alpha", "session_id": "session-alpha"}
NOW = "2026-10-14T00:00:00Z"
HTML = "<script>window.fakeExecuted = true</script>"
STOP_BUTTON = "STOP — остановить активного агента"


def stamp(minute):
    return f"2026-10-14T00:{minute:02d}:00Z"


def base(scope, name, minute):
    return {
        "id": name,
        "scope": scope,
        "schema_version": "1.0",
        "version": 1,
        "created_at": stamp(minute),
        "updated_at": stamp(minute),
    }


def source(name, text, minute, **over):
    row = {
        **base(WORK, name, minute),
        "source_id": name,
        "provenance": "operator_input",
        "occurred_at": stamp(minute),
        "author_id": "operator",
        "content": text,
        "availability": "available",
    }
    row.update(over)
    return row


def utterance(name, text, minute, status="approved", revision=1):
    return {
        **base(WORK, name, minute),
        "utterance_id": name,
        "revision": revision,
        "content": text,
        "content_hash": "h" * 64,
        "audience_id": "operator",
        "channel_ids": ["panel"],
        "moderation_status": status,
        "moderation_check_id": "check-1" if status == "approved" else None,
    }


def delivery(utterance_id, status, minute=9, channel="panel"):
    return {
        **base(WORK, f"d-{utterance_id}-{channel}", minute),
        "utterance_id": utterance_id,
        "revision": 1,
        "channel_id": channel,
        "attempt_id": "attempt-1",
        "status": status,
    }


def run_row(state="ACTIVE"):
    return {
        **base(RUN_SCOPE, "run-alpha", 0),
        "agent_id": "primary",
        "run_id": "run-alpha",
        "session_id": "session-alpha",
        "control_epoch": 1,
        "state": state,
        "activity": "OPEN",
        "autonomy": "observation",
        "voice_enabled": False,
        "gm_paused": True,
        "unconfirmed_resource_ids": [],
    }


def page_of(items):
    return {"items": items, "limit": 50, "next_cursor": None, "complete": True}


def mock_dialogue(
    page,
    *,
    run="ACTIVE",
    sources=(),
    utterances=(),
    deliveries=(),
    operations=(),
    briefings=(),
    command=None,
):
    """``command`` receives the posted body and fulfills or aborts the request."""
    posted, state = [], {"briefings": list(briefings), "stopped": False}

    def route(request):
        path = urlsplit(request.request.url).path
        query = parse_qs(urlsplit(request.request.url).query)
        body = request.request.post_data_json if request.request.method == "POST" else None
        selected = query.get("persona_id", ["alpha"])[0]
        resources = {
            "/api/v1/sources": sources,
            "/api/v1/utterances": utterances,
            "/api/v1/deliveries": deliveries,
            "/api/v1/operations": operations,
            "/api/v1/briefings": state["briefings"],
        }
        if path == "/api/v1/auth/session":
            request.fulfill(
                json={
                    "principal_id": "operator",
                    "scopes": [WORK, TEST],
                    "expires_at": "2099-01-01T00:00:00Z",
                    "csrf_token": "s" * 43,
                }
            )
        elif path == "/api/v1/sessions":
            row = run_row(run) if selected == "alpha" and run else None
            request.fulfill(json=page_of([row] if row else []))
        elif path in resources:
            scope_ok = selected == "alpha"
            request.fulfill(json=page_of(list(resources[path]) if scope_ok else []))
        elif path == "/api/v1/commands":
            posted.append((body, request.request.headers))
            if command is None:
                request.fulfill(
                    json={
                        "request_id": body["request_id"],
                        "scope": body["scope"],
                        "status": "completed",
                    }
                )
            else:
                command(request, body, state)
        elif path == "/api/v1/agents/primary/stop":
            state["stopped"] = body
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
        else:
            request.fulfill(status=404)

    page.route("**/api/v1/**", route)
    page.route_web_socket("**/api/v1/events", lambda socket: socket.on_message(lambda _: None))
    return posted, state


def open_dialogue(page, origin, **kwargs):
    handles = mock_dialogue(page, **kwargs)
    page.goto(origin + "/#/dialogue")
    expect(page.get_by_role("heading", name="Разговор", level=1)).to_be_visible()
    return handles


def test_timeline_shows_each_status_and_text_stays_plain(page, live_panel):
    open_dialogue(
        page,
        live_panel,
        sources=[
            source("s-1", f"Привет {HTML}", 1),
            source("s-2", None, 2, availability="expired", content=None),
        ],
        utterances=[
            utterance("u-1", "Первый ответ", 3),
            utterance("u-2", "Закрытый ответ", 4, status="blocked"),
            utterance("u-3", "Неуверенный ответ", 5),
            utterance("u-4", "Черновик ответа", 6, status="draft"),
        ],
        deliveries=[delivery("u-1", "DELIVERED"), delivery("u-3", "UNKNOWN")],
    )
    timeline = page.get_by_role("list", name="Лента диалога")
    expect(timeline.get_by_role("listitem")).to_have_count(6)
    expect(timeline).to_contain_text("Привет <script>window.fakeExecuted = true</script>")
    assert page.evaluate("window.fakeExecuted") is None
    expect(timeline).to_contain_text("Текст недоступен.")
    expect(timeline).to_contain_text("Срок хранения истёк или текст удалён.")
    expect(timeline.get_by_text("Завершено", exact=True)).to_have_count(1)
    expect(timeline.get_by_text("Отменено / не отправлено", exact=True)).to_have_count(1)
    expect(timeline.get_by_text("Исход неизвестен", exact=True)).to_have_count(1)
    expect(timeline.get_by_text("Предварительно", exact=True)).to_have_count(1)
    expect(timeline.get_by_text("Сохранено", exact=True)).to_have_count(1)


def test_send_posts_one_command_and_clears_only_after_the_receipt(page, live_panel):
    posted, _ = open_dialogue(page, live_panel)
    box = page.get_by_label("Сообщение персонажу")
    send = page.get_by_role("button", name="Отправить", exact=True)
    expect(send).to_be_disabled()
    box.fill("Привет, как дела?")
    expect(send).to_be_enabled()
    send.click()
    expect(page.get_by_text("Сообщение принято и сохранено ядром.")).to_be_visible()
    expect(box).to_have_value("")
    assert len(posted) == 1
    body, headers = posted[0]
    assert body["type"] == "dialogue.message"
    assert body["arguments"] == {"text": "Привет, как дела?"}
    assert body["scope"] == RUN_SCOPE and body["target"] == "session-alpha"
    assert headers["x-csrf-token"] == "s" * 43


def test_lost_reply_is_checked_with_the_same_command_never_doubled(page, live_panel):
    attempts = []

    def command(request, body, state):
        attempts.append(body)
        if len(attempts) == 1:
            request.abort()  # the core may have saved it; the reply is lost
        else:
            request.fulfill(
                json={
                    "request_id": body["request_id"],
                    "scope": body["scope"],
                    "status": "completed",
                }
            )

    open_dialogue(page, live_panel, command=command)
    box = page.get_by_label("Сообщение персонажу")
    box.fill("Важное сообщение")
    page.get_by_role("button", name="Отправить", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Результат отправки неизвестен")
    expect(box).to_have_value("Важное сообщение")
    expect(box).to_be_disabled()
    expect(page.get_by_role("button", name="Отправить", exact=True)).to_be_disabled()
    assert len(attempts) == 1  # no automatic retry
    page.get_by_role("button", name="Проверить исход").click()
    expect(page.get_by_text("Сообщение принято и сохранено ядром.")).to_be_visible()
    assert len(attempts) == 2 and attempts[0] == attempts[1]  # same id, same body
    expect(box).to_have_value("")


def test_a_clear_refusal_keeps_the_text_and_offers_no_blind_retry(page, live_panel):
    def command(request, body, state):
        request.fulfill(status=403, json={"code": "ACCESS_DENIED", "message": "private-details"})

    posted, _ = open_dialogue(page, live_panel, command=command)
    box = page.get_by_label("Сообщение персонажу")
    box.fill("Текст остаётся")
    page.get_by_role("button", name="Отправить", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Доступ запрещён")
    expect(box).to_have_value("Текст остаётся")
    assert "private-details" not in page.content()
    assert page.get_by_role("button", name="Проверить исход").count() == 0
    assert len(posted) == 1


def test_stop_stays_available_and_targets_the_owner_while_a_reply_is_generated(page, live_panel):
    operation = {
        **base(WORK, "op-1", 7),
        "operation_id": "op-1",
        "status": "RUNNING",
        "deadline": "2099-01-01T00:00:00Z",
    }
    _, state = open_dialogue(page, live_panel, operations=[operation])
    expect(page.get_by_text("Генерируется", exact=True)).to_be_visible()
    stop = page.get_by_role("button", name=STOP_BUTTON)
    expect(stop).to_be_enabled()
    stop.click()
    expect(page.get_by_text("Допуски отозваны.", exact=False)).to_be_visible()
    assert state["stopped"]["scope"] == RUN_SCOPE and state["stopped"]["target"] == "primary"


def test_sending_is_closed_while_the_agent_is_not_working(page, live_panel):
    open_dialogue(page, live_panel, run="STOPPED")
    page.get_by_label("Сообщение персонажу").fill("Есть кто?")
    expect(page.get_by_role("button", name="Отправить", exact=True)).to_be_disabled()
    expect(page.get_by_role("status").filter(has_text="не будет обработано")).to_be_visible()


def test_goal_card_does_not_invent_a_goal_or_streamed_text(page, live_panel):
    operation = {
        **base(WORK, "op-1", 7),
        "operation_id": "op-1",
        "status": "WAITING",
        "waiting_reason": "жду ответа человека",
        "deadline": "2099-01-01T00:00:00Z",
    }
    open_dialogue(page, live_panel, operations=[operation])
    card = page.get_by_label("Текущая цель")
    expect(card).to_contain_text("Агент работает")
    expect(card).to_contain_text("Цель и основание не переданы ядром.")
    expect(card).to_contain_text("op-1: жду ответа человека")
    expect(card).to_contain_text("приватный поток ещё не подключён")


def test_briefings_create_cancel_and_backend_refusal_is_shown(page, live_panel):
    def command(request, body, state):
        if body["type"] == "briefing.create":
            request.fulfill(
                status=503, json={"code": "CAPABILITY_UNAVAILABLE", "message": "private"}
            )
        else:
            request.fulfill(
                json={
                    "request_id": body["request_id"],
                    "scope": body["scope"],
                    "status": "completed",
                }
            )

    active = {
        **base(WORK, "b-1", 5),
        "version": 3,
        "author_id": "operator",
        "type": "scenario",
        "content": "Играем в шахматы",
        "expires_at": "2099-01-01T00:00:00Z",
        "status": "active",
    }
    posted, _ = open_dialogue(page, live_panel, briefings=[active], command=command)
    expect(page.get_by_text("Играем в шахматы")).to_be_visible()
    page.get_by_label("Текст вводной").fill("Сегодня говорим только о парусах")
    page.get_by_role("button", name="Добавить вводную").click()
    expect(page.get_by_role("alert")).to_contain_text("Укажите срок действия.")
    assert not [b for b, _ in posted if b["type"] == "briefing.create"]  # nothing was sent
    soon = page.evaluate("new Date(Date.now() + 10 * 86400000).toISOString().slice(0, 16)")
    page.locator("#briefing-expiry").fill(soon)
    page.get_by_role("button", name="Добавить вводную").click()
    expect(page.get_by_role("alert")).to_contain_text("пока недоступна")
    create = next(body for body, _ in posted if body["type"] == "briefing.create")
    assert create["arguments"]["data"]["type"] == "context"
    assert create["arguments"]["data"]["content"] == "Сегодня говорим только о парусах"
    assert create["arguments"]["data"]["expires_at"].endswith("Z")
    assert "private" not in page.content()
    page.get_by_role("button", name="Отменить", exact=True).click()
    expect(page.get_by_role("button", name="Отменить", exact=True)).to_be_enabled()
    cancel = next(body for body, _ in posted if body["type"] == "briefing.cancel")
    assert cancel["expected_version"] == 3 and cancel["arguments"]["data"] == {"briefing_id": "b-1"}


def test_test_memory_is_labelled_and_never_gets_a_command_target(page, live_panel):
    posted, _ = open_dialogue(page, live_panel)
    page.get_by_role("combobox", name="Просматриваемый персонаж и память", exact=True).click()
    page.get_by_role("option", name="beta · ТЕСТ · beta-test", exact=True).click()
    expect(page.get_by_text("Тестовая память", exact=True)).to_be_visible()
    expect(page.get_by_text("Нет подтверждённого запуска в этой области.").first).to_be_visible()
    page.get_by_label("Сообщение персонажу").fill("Не уйдёт")
    expect(page.get_by_role("button", name="Отправить", exact=True)).to_be_disabled()
    assert posted == []


def test_narrow_window_keeps_stop_and_has_no_horizontal_scroll(page, live_panel):
    open_dialogue(
        page,
        live_panel,
        sources=[source("s-1", "Очень длинное сообщение " * 30, 1)],
        utterances=[utterance("u-1", "И длинный ответ " * 30, 2)],
        deliveries=[delivery("u-1", "DELIVERED")],
    )
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.get_by_role("button", name=STOP_BUTTON)).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_state_is_not_persisted_in_browser_storage(page, live_panel):
    open_dialogue(page, live_panel, sources=[source("s-1", "Секретный разговор", 1)])
    page.get_by_label("Сообщение персонажу").fill("Черновик не сохраняется")
    assert not page.evaluate("localStorage.length + sessionStorage.length")
    assert json.dumps(page.context.cookies()).count("Секретный") == 0
