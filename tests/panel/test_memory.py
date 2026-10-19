"""M4-04 acceptance: memory screen on deterministic projections (UI-only, no live backend)."""

from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

WORK = {
    "workspace_id": "synthetic",
    "persona_id": "alpha",
    "memory_space_id": "alpha-work",
    "memory_mode": "work",
}
TEST = {**WORK, "persona_id": "beta", "memory_space_id": "beta-test", "memory_mode": "test"}
RUN_SCOPE = {**WORK, "agent_id": "primary", "run_id": "run-alpha", "session_id": "session-alpha"}
HTML = "<script>window.fakeExecuted = true</script>"
STOP_BUTTON = "STOP — остановить активного агента"
FAR = "2099-01-01T00:00:00Z"


def stamp(minute):
    return f"2026-10-14T00:{minute:02d}:00Z"


def base(name, minute, version=1):
    return {
        "id": name,
        "scope": WORK,
        "schema_version": "1.0",
        "version": version,
        "created_at": stamp(minute),
        "updated_at": stamp(minute),
    }


def ref(name, availability="available"):
    return {"source_id": name, "version": 1, "scope": WORK, "availability": availability}


def source(name, text, minute, **over):
    row = {
        **base(name, minute),
        "source_id": name,
        "provenance": "third_party_message",
        "source_type": "chat",
        "adapter_id": "fixture",
        "adapter_version": "1",
        "occurred_at": stamp(minute),
        "registered_at": stamp(minute),
        "author_id": "ivan",
        "content": text,
        "availability": "available",
        "retention": {"policy_id": "conversation-30d", "expires_at": FAR, "protected": False},
        "read_allowed": True,
        "api_transfer_allowed": True,
        "public_disclosure_allowed": False,
    }
    row.update(over)
    return row


def claim(name, text, minute, sources=("s-1",), confidence=0.6, **over):
    row = {
        **base(name, minute),
        "subject_id": "ivan",
        "content": text,
        "knowledge_type": "reported",
        "sources": [ref(item) for item in sources],
        "confidence": confidence,
        "conflict_ids": [],
    }
    row.update(over)
    return row


def run_row(state="ACTIVE"):
    return {
        **base("run-alpha", 0),
        "scope": RUN_SCOPE,
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


def page_of(items, complete=True, cursor=None):
    return {"items": items, "limit": 50, "next_cursor": cursor, "complete": complete}


def plan_row(**over):
    row = {
        **base("plan-1", 20),
        "operation_type": "memory.delete",
        "consequences": "Будут удалены текст источника, выдержки, сводки и индексы.",
        "dependencies": [
            {"resource_id": "s-1", "version": 1},
            {"resource_id": "c-1", "version": 1},
        ],
        "expires_at": FAR,
    }
    row.update(over)
    return row


def mock_memory(
    page,
    *,
    sources=(),
    second_sources=(),
    episodes=(),
    claims=(),
    summaries=(),
    plan=None,
    operation_status="RUNNING",
    command=None,
    run_state="ACTIVE",
):
    posted, reads = [], []

    def route(request):
        parts = urlsplit(request.request.url)
        query = parse_qs(parts.query)
        path = parts.path
        body = request.request.post_data_json if request.request.method == "POST" else None
        selected = query.get("persona_id", ["alpha"])[0]
        reads.append((path, query))
        if path == "/api/v1/auth/session":
            request.fulfill(
                json={
                    "principal_id": "operator",
                    "scopes": [WORK, TEST],
                    "expires_at": FAR,
                    "csrf_token": "s" * 43,
                }
            )
        elif path == "/api/v1/sessions":
            request.fulfill(json=page_of([run_row(run_state)] if selected == "alpha" else []))
        elif path == "/api/v1/sources":
            if selected != "alpha":
                request.fulfill(json=page_of([]))
            elif query.get("cursor") == ["cur-1"]:
                request.fulfill(json=page_of(list(second_sources)))
            else:
                more = bool(second_sources)
                request.fulfill(
                    json=page_of(list(sources), complete=not more, cursor="cur-1" if more else None)
                )
        elif path in ("/api/v1/episodes", "/api/v1/claims", "/api/v1/summaries"):
            data = {
                "/api/v1/episodes": episodes,
                "/api/v1/claims": claims,
                "/api/v1/summaries": summaries,
            }[path]
            request.fulfill(json=page_of(list(data) if selected == "alpha" else []))
        elif path == "/api/v1/plans/plan-1" and plan is not None:
            request.fulfill(json=plan)
        elif path == "/api/v1/operations/op-9":
            request.fulfill(
                json={
                    **base("op-9", 21),
                    "operation_id": "op-9",
                    "status": operation_status,
                    "deadline": FAR,
                    "waiting_reason": "чистим индекс",
                }
            )
        elif path.startswith("/api/v1/operations"):
            request.fulfill(json=page_of([]))
        elif path in ("/api/v1/utterances", "/api/v1/deliveries", "/api/v1/briefings"):
            request.fulfill(json=page_of([]))
        elif path == "/api/v1/commands":
            posted.append(body)
            if command is not None:
                command(request, body, len(posted))
            elif body["type"].endswith(".preview"):
                request.fulfill(
                    json={
                        "request_id": body["request_id"],
                        "scope": body["scope"],
                        "status": "completed",
                        "resource_id": "plan-1",
                        "version": 1,
                    }
                )
            else:
                request.fulfill(
                    json={
                        "request_id": body["request_id"],
                        "scope": body["scope"],
                        "status": "waiting",
                        "operation_id": "op-9",
                    }
                )
        else:
            request.fulfill(status=404)

    page.route("**/api/v1/**", route)
    page.route_web_socket("**/api/v1/events", lambda socket: socket.on_message(lambda _: None))
    return posted, reads


def open_memory(page, origin, **kwargs):
    handles = mock_memory(page, **kwargs)
    page.goto(origin + "/#/memory")
    expect(page.get_by_role("heading", name="Память", level=1)).to_be_visible()
    return handles


def pick(page, text):
    page.get_by_role("button").filter(has_text=text).first.click()
    expect(page.get_by_label("Карточка записи")).to_contain_text(text)


def test_cards_show_kind_of_knowledge_retention_rights_and_plain_text(page, live_panel):
    open_memory(
        page,
        live_panel,
        sources=[
            source("s-1", f"Привет {HTML}", 2),
            source(
                "s-2",
                None,
                3,
                availability="expired",
                content=None,
                retention={
                    "policy_id": "conversation-30d",
                    "expires_at": stamp(5),
                    "protected": False,
                },
            ),
            source(
                "s-3",
                "Наблюдение с экрана",
                4,
                provenance="observation",
                retention={"policy_id": "pinned", "expires_at": None, "protected": True},
            ),
        ],
        claims=[claim("c-1", "Иван живёт у озера", 6)],
    )
    expect(page.get_by_role("list", name="Список записей").get_by_role("listitem")).to_have_count(4)
    pick(page, "Привет <script>")
    card = page.get_by_label("Карточка записи")
    expect(card).to_contain_text("Чужое сообщение")
    expect(card).to_contain_text("Хранится до")
    expect(card).to_contain_text("Публичное раскрытие: нет")
    assert page.evaluate("window.fakeExecuted") is None
    page.get_by_role("list", name="Список записей").get_by_role("button").filter(
        has_text="Срок хранения истёк"
    ).first.click()
    expect(card).to_contain_text("Содержимое недоступно")
    expect(card).to_contain_text("Срок хранения истёк")
    page.get_by_role("button").filter(has_text="Наблюдение с экрана").click()
    expect(card).to_contain_text("Защищено")


def test_derived_record_with_a_deleted_basis_says_so_and_shows_no_text(page, live_panel):
    open_memory(
        page,
        live_panel,
        sources=[source("s-1", None, 2, availability="deleted", content=None)],
        claims=[
            claim("c-1", "Старое утверждение", 6, sources=("s-1",)).copy()
            | {"sources": [ref("s-1", "deleted")]}
        ],
    )
    page.get_by_role("button").filter(has_text="Утверждение").first.click()
    card = page.get_by_label("Карточка записи")
    expect(card).to_contain_text("Удалено")
    expect(card).to_contain_text("Основание: 1 · удалено")
    expect(card).to_contain_text("Содержимое недоступно")
    assert "Старое утверждение" not in page.content()


def test_a_removed_source_with_leftover_text_is_refused_not_displayed(page, live_panel):
    open_memory(
        page,
        live_panel,
        sources=[source("s-1", "Остаток удалённого текста", 2, availability="deleted")],
    )
    expect(page.get_by_text("Часть памяти недоступна")).to_be_visible()
    assert "Остаток удалённого текста" not in page.content()


def test_filters_combine_and_say_that_search_is_over_loaded_records(page, live_panel):
    open_memory(
        page,
        live_panel,
        sources=[
            source("s-1", "Про озеро и лодки", 2),
            source("s-2", "Про горы", 3, author_id="petr"),
        ],
        claims=[claim("c-1", "Про озеро", 6)],
    )
    expect(page.get_by_text("Поиск идёт по уже загруженным записям")).to_be_visible()
    items = page.get_by_role("list", name="Список записей").get_by_role("listitem")
    expect(items).to_have_count(3)
    page.get_by_label("Текст").fill("озеро")
    expect(items).to_have_count(2)
    page.get_by_label("Человек / автор").fill("petr")
    expect(page.get_by_text("Ничего не подходит под фильтры.")).to_be_visible()
    page.get_by_role("button", name="Сбросить").click()
    expect(items).to_have_count(3)


def test_more_pages_continue_from_the_server_cursor(page, live_panel):
    _, reads = open_memory(
        page,
        live_panel,
        sources=[source("s-1", "Первая страница", 2)],
        second_sources=[source("s-2", "Вторая страница", 3)],
    )
    expect(page.get_by_text("Загружена не вся память.")).to_be_visible()
    items = page.get_by_role("list", name="Список записей").get_by_role("listitem")
    expect(items).to_have_count(1)
    page.get_by_role("button", name="Загрузить ещё").click()
    expect(items).to_have_count(2)
    assert any(q.get("cursor") == ["cur-1"] for path, q in reads if path == "/api/v1/sources")


def test_correction_lowers_certainty_with_the_seen_version_and_never_overwrites_a_conflict(
    page, live_panel
):
    def command(request, body, count):
        request.fulfill(status=409, json={"code": "VERSION_CONFLICT", "message": "private"})

    posted, _ = open_memory(
        page,
        live_panel,
        sources=[source("s-1", "Исходное сообщение", 2)],
        claims=[claim("c-1", "Иван живёт у озера", 6, confidence=0.6, version=4)],
        command=command,
        run_state="STOPPED",
    )
    page.get_by_role("button").filter(has_text="Утверждение").first.click()
    page.get_by_role("button", name="Исправить", exact=True).click()
    page.get_by_label("Основание исправления").fill("Он сам уточнил")
    page.get_by_label("Новая уверенность", exact=False).fill("0.9")
    page.get_by_role("button", name="Сохранить исправление").click()
    expect(page.get_by_role("alert").filter(has_text="только снизить")).to_be_visible()
    assert posted == []
    page.get_by_label("Новая уверенность", exact=False).fill("0.3")
    page.get_by_role("button", name="Сохранить исправление").click()
    expect(page.get_by_role("alert").filter(has_text="уже изменена")).to_be_visible()
    assert len(posted) == 1
    body = posted[0]
    assert body["type"] == "memory.correct" and body["expected_version"] == 4
    assert body["arguments"]["data"]["confidence"] == 0.3
    assert "private" not in page.content()


def test_delete_needs_a_plan_that_names_the_record_and_an_explicit_confirmation(page, live_panel):
    posted, reads = open_memory(
        page, live_panel, sources=[source("s-1", "Что удалить", 2)], plan=plan_row()
    )
    page.get_by_role("button").filter(has_text="Что удалить").first.click()
    page.get_by_role("button", name="Удалить", exact=True).first.click()
    page.get_by_role("button", name="Подготовить удаление").click()
    panel = page.get_by_label("План последствий")
    expect(panel).to_contain_text("Будут удалены текст источника")
    expect(panel).to_contain_text("Связанных записей: 1")
    execute = panel.get_by_role("button", name="Удалить", exact=True)
    expect(execute).to_be_disabled()
    panel.get_by_label("Я понимаю, что удалённое восстановить нельзя").check()
    expect(execute).to_be_enabled()
    assert [body["type"] for body in posted] == ["memory.delete.preview"]  # nothing executed yet
    assert posted[0]["arguments"]["data"] == {"source_ids": ["s-1"]}
    execute.click()
    status = page.get_by_label("Ход выполнения")
    expect(status).to_contain_text("чистим индекс")
    expect(status).to_contain_text("Ожидает: Удаление")
    assert [body["type"] for body in posted] == ["memory.delete.preview", "memory.delete.execute"]
    run = posted[1]
    assert (
        run["arguments"] == {"plan_id": "plan-1", "plan_version": 1}
        and run["expected_version"] == 1
    )
    assert run["scope"] == RUN_SCOPE
    assert any(path == "/api/v1/plans/plan-1" for path, _ in reads)


def test_progress_shows_done_only_when_the_owner_says_succeeded(page, live_panel):
    open_memory(
        page,
        live_panel,
        sources=[source("s-1", "Что удалить", 2)],
        plan=plan_row(),
        operation_status="SUCCEEDED",
    )
    page.get_by_role("button").filter(has_text="Что удалить").first.click()
    page.get_by_role("button", name="Удалить", exact=True).first.click()
    page.get_by_role("button", name="Подготовить удаление").click()
    panel = page.get_by_label("План последствий")
    panel.get_by_label("Я понимаю, что удалённое восстановить нельзя").check()
    panel.get_by_role("button", name="Удалить", exact=True).click()
    expect(page.get_by_label("Ход выполнения")).to_contain_text("Выполнено: Удаление")


@pytest.mark.parametrize(
    ("reason", "plan"),
    [
        ("не содержит", plan_row(dependencies=[{"resource_id": "other", "version": 1}])),
        ("Срок плана истёк", plan_row(expires_at="2020-01-01T00:00:00Z")),
    ],
)
def test_a_plan_without_the_chosen_record_or_expired_cannot_be_executed(
    page, live_panel, reason, plan
):
    posted, _ = open_memory(page, live_panel, sources=[source("s-1", "Что удалить", 2)], plan=plan)
    page.get_by_role("button").filter(has_text="Что удалить").first.click()
    page.get_by_role("button", name="Удалить", exact=True).first.click()
    page.get_by_role("button", name="Подготовить удаление").click()
    panel = page.get_by_label("План последствий")
    expect(panel.get_by_role("alert")).to_contain_text(reason)
    panel.get_by_label("Я понимаю, что удалённое восстановить нельзя").check()
    expect(panel.get_by_role("button", name="Удалить", exact=True)).to_be_disabled()
    assert [body["type"] for body in posted] == ["memory.delete.preview"]


def test_lost_reply_on_execution_is_checked_with_the_same_command(page, live_panel):
    attempts = []

    def command(request, body, count):
        if body["type"].endswith(".preview"):
            request.fulfill(
                json={
                    "request_id": body["request_id"],
                    "scope": body["scope"],
                    "status": "completed",
                    "resource_id": "plan-1",
                    "version": 1,
                }
            )
        else:
            attempts.append(body)
            if len(attempts) == 1:
                request.abort()
            else:
                request.fulfill(
                    json={
                        "request_id": body["request_id"],
                        "scope": body["scope"],
                        "status": "waiting",
                        "operation_id": "op-9",
                    }
                )

    open_memory(
        page,
        live_panel,
        sources=[source("s-1", "Что удалить", 2)],
        plan=plan_row(),
        command=command,
    )
    page.get_by_role("button").filter(has_text="Что удалить").first.click()
    page.get_by_role("button", name="Удалить", exact=True).first.click()
    page.get_by_role("button", name="Подготовить удаление").click()
    panel = page.get_by_label("План последствий")
    panel.get_by_label("Я понимаю, что удалённое восстановить нельзя").check()
    panel.get_by_role("button", name="Удалить", exact=True).click()
    expect(panel.get_by_role("alert")).to_contain_text("Результат неизвестен")
    assert len(attempts) == 1
    panel.get_by_role("button", name="Проверить исход").click()
    expect(page.get_by_label("Ход выполнения")).to_be_visible()
    assert len(attempts) == 2 and attempts[0] == attempts[1]


def test_export_uses_its_own_commands_and_is_not_a_publication(page, live_panel):
    posted, _ = open_memory(
        page,
        live_panel,
        sources=[source("s-1", "Что выгрузить", 2)],
        plan=plan_row(operation_type="memory.export"),
    )
    page.get_by_role("button").filter(has_text="Что выгрузить").first.click()
    page.get_by_role("button", name="Экспорт", exact=True).click()
    page.get_by_role("button", name="Подготовить экспорт").click()
    panel = page.get_by_label("План последствий")
    expect(panel).to_contain_text("Внешняя публикация")
    panel.get_by_role("button", name="Экспортировать").click()
    expect(page.get_by_label("Ход выполнения")).to_be_visible()
    assert [body["type"] for body in posted] == ["memory.export.preview", "memory.export.execute"]


def test_test_memory_is_labelled_and_commands_are_closed_without_a_run(page, live_panel):
    open_memory(page, live_panel, sources=[source("s-1", "Рабочая запись", 2)])
    page.get_by_role("combobox", name="Просматриваемый персонаж и память", exact=True).click()
    page.get_by_role("option", name="beta · ТЕСТ · beta-test", exact=True).click()
    expect(page.get_by_text("Тестовая память", exact=True)).to_be_visible()
    expect(page.get_by_text("Память пока пуста.")).to_be_visible()


def test_people_tab_is_the_people_screen_not_a_placeholder(page, live_panel):
    open_memory(page, live_panel)
    page.get_by_role("tab", name="Люди").click()
    expect(page.get_by_text("Людей в памяти пока нет.")).to_be_visible()
    assert "готовится отдельным этапом" not in page.content()


def test_narrow_window_keeps_stop_and_has_no_horizontal_scroll(page, live_panel):
    open_memory(
        page,
        live_panel,
        sources=[source("s-1", "Очень длинная запись " * 40, 2)],
        claims=[claim("c-1", "Длинное утверждение " * 30, 6)],
    )
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.get_by_role("button", name=STOP_BUTTON)).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
