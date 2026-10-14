"""M4-08 acceptance: LLM and cost dashboard on deterministic projections (UI-only)."""

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
FAR = "2099-01-01T00:00:00Z"
STOP_BUTTON = "STOP — остановить активного агента"


def stamp(minute, day=14):
    return f"2026-10-{day:02d}T00:{minute:02d}:00Z"


def base(name, minute=1, version=1):
    return {
        "id": name,
        "scope": WORK,
        "schema_version": "1.0",
        "version": version,
        "created_at": stamp(minute),
        "updated_at": stamp(minute),
    }


def usage(**over):
    row = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cached_input_tokens": None,
        "reasoning_output_tokens": None,
        "source": "provider",
        "completeness": "complete",
    }
    row.update(over)
    return row


def attempt(name, minute=2, day=14, **over):
    row = {
        **base("a-" + name, minute),
        "operation_id": "op-" + name,
        "attempt_id": name,
        "role_id": "character",
        "assignment_id": "asg-1",
        "assignment_version": 1,
        "requested_model_id": "model-x",
        "reported_model_id": None,
        "status": "completed",
        "started_at": stamp(minute, day),
        "completed_at": stamp(minute + 1, day),
        "usage": usage(),
        "tariff_id": "t1",
        "tariff_version": 1,
    }
    row.update(over)
    return row


def cost(name, amount="0.012", currency="USD", completeness="complete"):
    return {
        **base("c-" + name),
        "attempt_id": name,
        "tariff_id": "t1" if amount else None,
        "tariff_version": 1 if amount else None,
        "amount": amount,
        "currency": currency,
        "completeness": completeness,
        "provenance": "provider_usage",
    }


def tariff(name="t1", version=1, **over):
    row = {
        **base(name, version=version),
        "connection_id": "conn-1",
        "model_id": "model-x",
        "unit_tokens": 1000000,
        "currency": "USD",
        "input_rate": "2",
        "output_rate": "6",
        "cached_input_rate": None,
        "reasoning_output_rate": None,
        "effective_at": stamp(0),
    }
    row.update(over)
    return row


def assignment(name="asg-1", **over):
    row = {
        **base(name),
        "role_id": "character",
        "connection_id": "conn-1",
        "model_id": "model-x",
        "parameters": {"schema_id": "p", "data": {}},
        "required_capabilities": [],
        "capability_verification": "verified",
        "fallback_assignment_id": None,
    }
    row.update(over)
    return row


def connection():
    return {
        **base("conn-1"),
        "name": "Агрегатор",
        "adapter_id": "openai",
        "adapter_version": "1",
        "endpoint_setting": "AI_PLATFORM_URL",
        "credential_setting": "AI_PLATFORM_KEY",
        "configured": True,
        "verification": "verified",
        "capabilities": [],
    }


def run_row():
    return {
        **base("run-alpha", 0, version=4),
        "scope": RUN_SCOPE,
        "agent_id": "primary",
        "run_id": "run-alpha",
        "session_id": "session-alpha",
        "control_epoch": 1,
        "state": "ACTIVE",
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
        "operation_type": "llm.recalculate",
        "consequences": "Будут пересчитаны 12 расчётов с версией тарифа v1.",
        "dependencies": [{"resource_id": "t1", "version": 1}, {"resource_id": "a-x", "version": 1}],
        "expires_at": FAR,
    }
    row.update(over)
    return row


def mock_llm(
    page,
    *,
    attempts=(),
    more_attempts=(),
    costs=(),
    tariffs=(),
    assignments=(),
    plan=None,
    command=None,
):
    posted, reads = [], []

    def route(request):
        parts = urlsplit(request.request.url)
        query = parse_qs(parts.query)
        path = parts.path
        body = request.request.post_data_json if request.request.method == "POST" else None
        selected = query.get("persona_id", ["alpha"])[0]
        reads.append((path, query))
        mine = selected == "alpha"
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
            request.fulfill(json=page_of([run_row()] if mine else []))
        elif path == "/api/v1/llm-attempts":
            if not mine:
                request.fulfill(json=page_of([]))
            elif query.get("cursor") == ["cur-1"]:
                request.fulfill(json=page_of(list(more_attempts)))
            else:
                more = bool(more_attempts)
                request.fulfill(
                    json=page_of(
                        list(attempts), complete=not more, cursor="cur-1" if more else None
                    )
                )
        elif path in (
            "/api/v1/cost-calculations",
            "/api/v1/tariffs",
            "/api/v1/llm-assignments",
            "/api/v1/connections",
        ):
            data = {
                "/api/v1/cost-calculations": costs,
                "/api/v1/tariffs": tariffs,
                "/api/v1/llm-assignments": assignments,
                "/api/v1/connections": [connection()],
            }
            request.fulfill(json=page_of(list(data[path]) if mine else []))
        elif path == "/api/v1/plans/plan-1" and plan is not None:
            request.fulfill(json=plan)
        elif path == "/api/v1/operations/op-9":
            request.fulfill(
                json={
                    **base("op-9", 21),
                    "operation_id": "op-9",
                    "status": "RUNNING",
                    "deadline": FAR,
                    "waiting_reason": "пересчитываем",
                }
            )
        elif path.startswith("/api/v1/operations"):
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
            elif body["type"].endswith(".execute"):
                request.fulfill(
                    json={
                        "request_id": body["request_id"],
                        "scope": body["scope"],
                        "status": "waiting",
                        "operation_id": "op-9",
                    }
                )
            else:
                request.fulfill(
                    json={
                        "request_id": body["request_id"],
                        "scope": body["scope"],
                        "status": "completed",
                    }
                )
        else:
            request.fulfill(status=404)

    page.route("**/api/v1/**", route)
    page.route_web_socket("**/api/v1/events", lambda socket: socket.on_message(lambda _: None))
    return posted, reads


def open_llm(page, origin, **kwargs):
    handles = mock_llm(page, **kwargs)
    page.goto(origin + "/#/llm")
    expect(page.get_by_role("heading", name="LLM и расходы", level=1)).to_be_visible()
    return handles


def tab(page, name):
    page.get_by_role("tab", name=name, exact=True).click()


def test_overview_keeps_currencies_apart_and_unknown_cost_out_of_the_sum(page, live_panel):
    open_llm(
        page,
        live_panel,
        attempts=[
            attempt("a"),
            attempt("b", 3),
            attempt("c", 4),
            attempt("d", 5),
            attempt("e", 6, operation_id="op-a"),
        ],
        costs=[cost("a", "0.012"), cost("b", "0.5", "EUR"), cost("c", None, None, "unknown")],
        assignments=[assignment()],
    )
    money = page.get_by_label("Расходы по валютам")
    expect(money).to_contain_text("0.5 EUR")
    expect(money).to_contain_text("0.012 USD")
    expect(money).to_contain_text("Цена неизвестна у 3 попыток")
    expect(money).to_contain_text("Валюты не складываются")
    assert "0.512" not in page.content()
    expect(page.get_by_text("Запросы / попытки").locator("xpath=..")).to_contain_text("4 / 5")
    expect(page.get_by_text("Повторы").locator("xpath=..")).to_contain_text("1")


def test_completeness_is_always_visible_and_says_what_is_missing(page, live_panel):
    open_llm(
        page,
        live_panel,
        attempts=[
            attempt("a"),
            attempt(
                "b",
                3,
                usage=usage(
                    source="unknown", completeness="unknown", input_tokens=None, output_tokens=None
                ),
                status="unknown",
            ),
            attempt("c", 4, usage=usage(completeness="partial"), status="incomplete"),
        ],
        costs=[cost("a"), cost("c", "0.1", "USD", "partial")],
        assignments=[assignment()],
    )
    status = page.get_by_label("Полнота статистики")
    expect(status).to_contain_text("Неполная")
    expect(status).to_contain_text("Usage неизвестен у 1 попыток")
    expect(status).to_contain_text("Usage неполный у 1 попыток")
    expect(status).to_contain_text("USD: 1 расчётов частичные")


def test_more_pages_continue_from_the_cursor_and_the_total_says_it_is_partial(page, live_panel):
    _, reads = open_llm(
        page,
        live_panel,
        attempts=[attempt("a")],
        more_attempts=[attempt("b", 3)],
        costs=[cost("a")],
        assignments=[assignment()],
    )
    status = page.get_by_label("Полнота статистики")
    expect(status).to_contain_text("Загружена не вся история")
    expect(page.get_by_text("Запросы / попытки").locator("xpath=..")).to_contain_text("1 / 1")
    page.get_by_role("button", name="Загрузить ещё").click()
    expect(page.get_by_text("Запросы / попытки").locator("xpath=..")).to_contain_text("2 / 2")
    assert any(q.get("cursor") == ["cur-1"] for path, q in reads if path == "/api/v1/llm-attempts")


def test_filters_narrow_the_totals(page, live_panel):
    open_llm(
        page,
        live_panel,
        attempts=[
            attempt("a", 2, 13),
            attempt("b", 3, 14, role_id="gm", requested_model_id="model-y", assignment_id="asg-2"),
        ],
        costs=[cost("a"), cost("b", "0.4")],
        assignments=[assignment(), assignment("asg-2", role_id="gm", model_id="model-y")],
    )
    pairs = page.get_by_text("Запросы / попытки").locator("xpath=..")
    expect(pairs).to_contain_text("2 / 2")
    page.get_by_label("С даты").fill("2026-10-14")
    expect(pairs).to_contain_text("1 / 1")
    expect(page.get_by_label("Расходы по валютам")).to_contain_text("0.4 USD")
    page.get_by_role("button", name="Сбросить").click()
    expect(pairs).to_contain_text("2 / 2")
    page.get_by_role("combobox", name="Роль").click()
    page.get_by_role("option", name="character", exact=True).click()
    expect(pairs).to_contain_text("1 / 1")


def test_history_shows_routing_unknowns_and_never_a_prompt(page, live_panel):
    open_llm(
        page,
        live_panel,
        attempts=[
            attempt("a", 2, reported_model_id="model-y"),
            attempt(
                "b",
                5,
                completed_at=None,
                tariff_id=None,
                tariff_version=None,
                usage=usage(
                    source="unknown", completeness="unknown", input_tokens=None, output_tokens=None
                ),
            ),
        ],
        costs=[cost("a", "0.5", "EUR", "partial")],
        assignments=[assignment()],
    )
    tab(page, "История")
    table = page.get_by_role("table", name="История попыток")
    rows = table.get_by_role("row")
    expect(rows).to_have_count(3)
    expect(rows.nth(1)).to_contain_text("Usage неизвестен")
    expect(rows.nth(1)).to_contain_text("тариф не закреплён")
    expect(rows.nth(1)).to_contain_text("неизвестно")
    expect(rows.nth(2)).to_contain_text("ответила: model-y")
    expect(rows.nth(2)).to_contain_text("0.5 EUR (частично)")
    expect(rows.nth(2)).to_contain_text("60 000 мс")
    expect(page.get_by_text("Полные prompt, тексты приватных диалогов")).to_be_visible()


def test_roles_show_the_binding_and_the_reserve_read_only(page, live_panel):
    open_llm(
        page,
        live_panel,
        attempts=[attempt("a")],
        assignments=[
            assignment(fallback_assignment_id="asg-r"),
            assignment("asg-r", model_id="model-r", role_id="character"),
        ],
    )
    tab(page, "Модели и роли")
    table = page.get_by_role("table", name="Назначения ролей")
    expect(table.get_by_role("row")).to_have_count(2)
    expect(table).to_contain_text("model-x")
    expect(table).to_contain_text("model-r · Агрегатор")
    expect(table).to_contain_text("проверено")
    expect(page.get_by_text("только для чтения")).to_be_visible()


def test_prices_show_versions_empty_is_not_zero_and_the_new_version_is_a_command(page, live_panel):
    posted, _ = open_llm(
        page,
        live_panel,
        assignments=[assignment()],
        tariffs=[
            tariff(),
            tariff("t2", 2, effective_at=stamp(10), input_rate="3", output_rate=None),
        ],
    )
    tab(page, "Цены")
    expect(page.get_by_text("v2 · с")).to_be_visible()
    expect(page.get_by_text("выход не задана", exact=False)).to_be_visible()
    expect(page.get_by_text("действует", exact=True)).to_have_count(1)
    form = page.get_by_label("Новая версия тарифа")
    form.get_by_role("button", name="Сохранить тариф").click()
    expect(form.get_by_role("alert")).to_contain_text("подключение")
    form.get_by_label("Подключение").click()
    page.get_by_role("option", name="Агрегатор", exact=True).click()
    form.get_by_label("ID модели").fill("model-x")
    form.get_by_label("Валюта").fill("usd")
    form.get_by_role("button", name="Сохранить тариф").click()
    expect(form.get_by_role("alert")).to_contain_text("не задана")
    form.get_by_label("Вход (за единицу)").fill("2.5")
    form.get_by_label("Выход (за единицу)").fill("")
    page.locator("#tariff-effective").fill("2020-01-01T00:00")
    expect(form).to_contain_text("прежние расчёты не изменятся")
    form.get_by_role("button", name="Сохранить тариф").click()
    expect(form.get_by_text("Тариф принят.")).to_be_visible()
    command = next(body for body in posted if body["type"] == "tariff.create")
    data = command["arguments"]["data"]
    assert data["input_rate"] == "2.5" and "output_rate" not in data
    assert data["currency"] == "USD" and data["unit_tokens"] == 1000000
    assert data["effective_at"].endswith("Z") and command["scope"] == RUN_SCOPE


def test_repricing_is_a_separate_explicit_plan_bound_to_the_tariff(page, live_panel):
    posted, _ = open_llm(
        page, live_panel, assignments=[assignment()], tariffs=[tariff()], plan=plan_row()
    )
    tab(page, "Цены")
    page.get_by_text("Пересчёт по действующей версии").click()
    page.get_by_role("button", name="Подготовить пересчёт по этому тарифу").click()
    panel = page.get_by_label("План последствий")
    expect(panel).to_contain_text("Будут пересчитаны 12 расчётов")
    expect(panel).to_contain_text("не стирая прежние")
    assert [body["type"] for body in posted] == ["llm.recalculate.preview"]
    panel.get_by_role("button", name="Выполнить пересчёт").click()
    expect(page.get_by_label("Ход выполнения")).to_contain_text("пересчитываем")
    assert posted[1]["type"] == "llm.recalculate.execute"
    assert posted[1]["arguments"] == {"plan_id": "plan-1", "plan_version": 1}
    assert posted[0]["arguments"]["data"] == {"tariff_id": "t1", "tariff_version": 1}


@pytest.mark.parametrize(
    ("reason", "plan"),
    [
        (
            "не содержит выбранный тариф",
            plan_row(dependencies=[{"resource_id": "other", "version": 1}]),
        ),
        ("Срок плана истёк", plan_row(expires_at="2020-01-01T00:00:00Z")),
    ],
)
def test_a_repricing_plan_for_another_tariff_or_expired_cannot_run(page, live_panel, reason, plan):
    open_llm(page, live_panel, assignments=[assignment()], tariffs=[tariff()], plan=plan)
    tab(page, "Цены")
    page.get_by_text("Пересчёт по действующей версии").click()
    page.get_by_role("button", name="Подготовить пересчёт по этому тарифу").click()
    panel = page.get_by_label("План последствий")
    expect(panel.get_by_role("alert")).to_contain_text(reason)
    expect(panel.get_by_role("button", name="Выполнить пересчёт")).to_be_disabled()


def test_analytics_give_a_chart_and_the_same_numbers_as_a_table(page, live_panel):
    open_llm(
        page,
        live_panel,
        attempts=[
            attempt("a", 2, 13),
            attempt("b", 3, 14, role_id="gm"),
            attempt(
                "c",
                4,
                14,
                usage=usage(
                    source="unknown", completeness="unknown", input_tokens=None, output_tokens=None
                ),
            ),
        ],
        costs=[cost("a", "0.1"), cost("b", "0.2")],
        assignments=[assignment()],
    )
    tab(page, "Аналитика")
    expect(page.get_by_role("img", name="Известные токены по дням")).to_be_visible()
    expect(page.get_by_role("img", name="Расход по дням в USD")).to_be_visible()
    page.get_by_text("Таблица данных").first.click()
    expect(page.get_by_text("2026-10-14").first).to_be_visible()
    expect(page.get_by_text("неизвестно у 1 попыток")).to_be_visible()
    expect(page.get_by_text("character: ")).to_be_visible()


def test_empty_state_says_unknown_is_not_zero(page, live_panel):
    open_llm(page, live_panel)
    expect(page.get_by_label("Расходы по валютам")).to_contain_text(
        "Известной стоимости нет. Это не нулевой расход"
    )
    tab(page, "Цены")
    expect(page.get_by_text("Тарифов нет: стоимость неизвестна, а не нулевая.")).to_be_visible()
    tab(page, "Модели и роли")
    expect(page.get_by_text("Без назначенной модели реальная генерация выключена.")).to_be_visible()


def test_no_secret_names_appear_and_test_memory_is_labelled(page, live_panel):
    open_llm(
        page, live_panel, attempts=[attempt("a")], costs=[cost("a")], assignments=[assignment()]
    )
    content = page.content()
    assert "AI_PLATFORM_KEY" not in content and "AI_PLATFORM_URL" not in content
    page.get_by_role("combobox", name="Просматриваемый персонаж и память", exact=True).click()
    page.get_by_role("option", name="beta · ТЕСТ · beta-test", exact=True).click()
    expect(page.get_by_text("Тестовая память", exact=True)).to_be_visible()
    expect(
        page.get_by_text("реальные API-вызовы лаборатории входят в общий расход")
    ).to_be_visible()


def test_narrow_window_keeps_stop_and_has_no_horizontal_scroll(page, live_panel):
    open_llm(
        page, live_panel, attempts=[attempt("a")], costs=[cost("a")], assignments=[assignment()]
    )
    tab(page, "История")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.get_by_role("button", name=STOP_BUTTON)).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
