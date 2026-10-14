"""M4-05 acceptance: story and development tab on deterministic projections (UI-only)."""

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


def stamp(minute):
    return f"2026-10-14T00:{minute:02d}:00Z"


def base(name, minute=1, version=1):
    return {
        "id": name,
        "scope": WORK,
        "schema_version": "1.0",
        "version": version,
        "created_at": stamp(minute),
        "updated_at": stamp(minute),
    }


def ref(availability="available"):
    return {"source_id": "s-1", "version": 1, "scope": WORK, "availability": availability}


def arc(name="arc-1", status="open", direction="Найти причину страха воды", minute=1, **over):
    row = {
        **base(name, minute),
        "direction": direction,
        "boundaries": "Без насилия и принуждения",
        "status": status,
        "grounds": [ref()],
        "episode_ids": [],
        "cooldown_until": None,
        "rules_version": 1,
    }
    row.update(over)
    return row


def opportunity(name, status, minute=2, **over):
    row = {
        **base(name, minute),
        "arc_id": "arc-1",
        "grounds": [ref()],
        "applicability": "Предложить прогулку к озеру " + name,
        "boundaries": "Без принуждения",
        "expires_at": FAR,
        "capability_ids": [],
        "status": status,
        "choice": None,
        "actual_outcome": None,
        "reason_code": None,
        "rolls": [],
    }
    row.update(over)
    return row


def change(name, status, minute=3, version=1, **over):
    row = {
        **base(name, minute, version),
        "proposal_id": "p-" + name,
        "owner": "persona",
        "base_version": 3,
        "new_version": 4 if status == "APPLIED" else None,
        "patch": {
            "schema_id": "development.patch",
            "data": {"property": "trust", "before": 0.4, "after": 0.5, "kind": "change"},
        },
        "evidence": [ref()],
        "interpretation": "Он дважды помог",
        "uncertainty": 0.3,
        "expected_manifestation": "Охотнее доверяет",
        "status": status,
        "checks": [],
    }
    row.update(over)
    return row


def run_row(gm_paused=True):
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
        "gm_paused": gm_paused,
        "unconfirmed_resource_ids": [],
    }


def page_of(items):
    return {"items": items, "limit": 50, "next_cursor": None, "complete": True}


def plan_row(**over):
    row = {
        **base("plan-1", 20),
        "operation_type": "change.rollback",
        "consequences": "Будет создана компенсирующая версия свойства trust.",
        "dependencies": [
            {"resource_id": "c-1", "version": 1},
            {"resource_id": "o-9", "version": 1},
        ],
        "expires_at": FAR,
    }
    row.update(over)
    return row


def mock_development(
    page, *, arcs=(), opportunities=(), changes=(), plan=None, command=None, gm_paused=True
):
    posted = []

    def route(request):
        parts = urlsplit(request.request.url)
        query = parse_qs(parts.query)
        path = parts.path
        body = request.request.post_data_json if request.request.method == "POST" else None
        mine = query.get("persona_id", ["alpha"])[0] == "alpha"
        data = {
            "/api/v1/arcs": arcs,
            "/api/v1/opportunities": opportunities,
            "/api/v1/changes": changes,
        }
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
            request.fulfill(json=page_of([run_row(gm_paused)] if mine else []))
        elif path in data:
            request.fulfill(json=page_of(list(data[path]) if mine else []))
        elif path == "/api/v1/character-states":
            request.fulfill(
                json=page_of(
                    [
                        {
                            **base("state-1"),
                            "persona_id": "alpha",
                            "profile_version": 2,
                            "state_version": 3,
                            "values": {"schema_id": "state", "data": {"trust": 0.5}},
                        }
                    ]
                    if mine
                    else []
                )
            )
        elif path == "/api/v1/personas":
            request.fulfill(
                json=page_of(
                    [
                        {
                            **base("profile-1"),
                            "persona_id": "alpha",
                            "canon": "Канон",
                            "style": "Стиль",
                            "accepted_revision_id": "rev-1",
                            "initial_values": {},
                            "property_definitions": {"trust": {"autonomy": "experience"}},
                            "development_bounds": {"trust": {"step": 0.1}},
                        }
                    ]
                    if mine
                    else []
                )
            )
        elif path == "/api/v1/plans/plan-1" and plan is not None:
            request.fulfill(json=plan)
        elif path == "/api/v1/operations/op-9":
            request.fulfill(
                json={
                    **base("op-9", 21),
                    "operation_id": "op-9",
                    "status": "RUNNING",
                    "deadline": FAR,
                    "waiting_reason": "создаём компенсирующую версию",
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
    return posted


def open_development(page, origin, **kwargs):
    posted = mock_development(page, **kwargs)
    page.goto(origin + "/#/development")
    expect(page.get_by_role("heading", name="Сюжет и развитие", level=1)).to_be_visible()
    return posted


def tab(page, name):
    page.get_by_role("tab", name=name, exact=True).click()


def test_overview_separates_attention_the_path_and_the_game_master(page, live_panel):
    open_development(
        page,
        live_panel,
        changes=[
            change("c-1", "AWAITING_APPROVAL", 5),
            change("c-2", "CONFLICT", 6),
            change("c-3", "APPLIED", 7),
        ],
        opportunities=[
            opportunity("o-1", "BLOCKED", reason_code="граница-насилие"),
            opportunity(
                "o-2", "RESOLVED", 4, choice="пошёл к озеру", actual_outcome="вернулся спокойным"
            ),
            opportunity("o-3", "OFFERED", 8),
        ],
    )
    queue = page.get_by_label("Требует внимания")
    expect(queue).to_contain_text("Решение по изменению trust")
    expect(queue).to_contain_text("Конфликт версий: trust")
    expect(queue).to_contain_text("Граница: граница-насилие")
    path = page.get_by_label("Путь развития")
    expect(path.get_by_role("listitem")).to_have_count(2)
    expect(path).to_contain_text("Пережито")
    expect(path).to_contain_text("пошёл к озеру")
    expect(path).to_contain_text("trust: 0.4 → 0.5")
    expect(path).not_to_contain_text("Предложить прогулку")  # a possibility is not on the path
    expect(page.get_by_label("Гейммастер")).to_contain_text("На паузе")


def test_game_master_controls_use_the_seen_version(page, live_panel):
    posted = open_development(page, live_panel, gm_paused=True)
    box = page.get_by_label("Гейммастер")
    box.get_by_role("button", name="Снять паузу").click()
    expect(box.get_by_text("Команда принята ядром.")).to_be_visible()
    assert posted[0]["type"] == "gm.configure" and posted[0]["expected_version"] == 4
    assert posted[0]["arguments"]["data"] == {"paused": False}
    box.get_by_label("Интенсивность").click()
    page.get_by_role("option", name="Высокая", exact=True).click()
    box.get_by_role("button", name="Применить интенсивность").click()
    expect(page.get_by_text("Команда принята ядром.").first).to_be_visible()
    assert posted[-1]["arguments"]["data"] == {"intensity": "high"}
    expect(box).to_contain_text("нет собственного голоса")


def test_arc_card_keeps_lived_possible_and_blocked_apart(page, live_panel):
    open_development(
        page,
        live_panel,
        arcs=[arc()],
        opportunities=[
            opportunity("o-1", "OFFERED", 8),
            opportunity("o-2", "RESOLVED", 4, choice="пошёл", actual_outcome="вернулся"),
            opportunity("o-3", "BLOCKED", reason_code="граница-принуждение"),
        ],
    )
    tab(page, "Линии")
    page.get_by_role("button").filter(has_text="Найти причину страха воды").click()
    card = page.get_by_label("Карточка линии")
    expect(card).to_contain_text("Направление, а не предсказанный финал")
    expect(card).to_contain_text("Заблокировано границами")
    expect(card).to_contain_text("Есть блокировки")
    expect(card).to_contain_text("Выбор: пошёл")
    expect(card).to_contain_text("Исход: вернулся")
    text = card.inner_text()
    lived = text.split("Пережито")[1].split("Возможные продолжения")[0]
    possible = text.split("Возможные продолжения (ещё не произошли)")[1].split("Заблокировано")[0]
    assert "вернулся" in lived and "Предложить прогулку" not in lived
    assert "Предложить прогулку к озеру o-1" in possible and "вернулся" not in possible


def test_cancel_a_possibility_and_pause_the_arc(page, live_panel):
    posted = open_development(
        page,
        live_panel,
        arcs=[arc(version=2)],
        opportunities=[opportunity("o-1", "OFFERED", version=6)],
    )
    tab(page, "Линии")
    page.get_by_role("button").filter(has_text="Найти причину страха воды").click()
    card = page.get_by_label("Карточка линии")
    card.get_by_role("button", name="Отменить", exact=True).click()
    expect(card.get_by_text("Команда принята ядром.")).to_be_visible()
    assert posted[0]["type"] == "opportunity.cancel" and posted[0]["expected_version"] == 6
    assert posted[0]["arguments"]["data"] == {"opportunity_id": "o-1"}
    card.get_by_role("button", name="Приостановить линию").click()
    assert posted[1]["type"] == "arc.pause" and posted[1]["expected_version"] == 2


def test_paused_and_closed_arcs_offer_no_pause_and_say_what_a_pause_means(page, live_panel):
    open_development(page, live_panel, arcs=[arc(status="paused")])
    tab(page, "Линии")
    page.get_by_role("button").filter(has_text="Найти причину страха воды").click()
    card = page.get_by_label("Карточка линии")
    expect(card).to_contain_text("позднее предложение её не оживляет")
    expect(card.get_by_role("button", name="Приостановить линию")).to_have_count(0)


def test_change_card_compares_values_and_keeps_expectation_apart_from_fact(page, live_panel):
    open_development(page, live_panel, changes=[change("c-1", "APPLIED", version=5)])
    tab(page, "Изменения")
    page.get_by_role("button").filter(has_text="trust: 0.4 → 0.5").click()
    card = page.get_by_label("Карточка изменения")
    expect(card).to_contain_text("Было: 0.4")
    expect(card).to_contain_text("Стало: 0.5")
    expect(card).to_contain_text("Интерпретация (это не факт)")
    expect(card).to_contain_text("Ожидаемое проявление (ещё не подтверждено)")
    expect(card).to_contain_text("Наблюдённые позднее проявления: связанных записей нет.")
    expect(card).to_contain_text("v3 → v4")
    expect(card).to_contain_text("Применено")


def test_lost_evidence_and_unrecognised_patch_are_not_presented_as_confirmed(page, live_panel):
    open_development(
        page,
        live_panel,
        changes=[
            change("c-1", "APPLIED", evidence=[ref("deleted")]),
            change("c-2", "READY", 4, patch={"schema_id": "x", "data": "mystery"}),
        ],
    )
    tab(page, "Изменения")
    page.get_by_role("button").filter(has_text="trust: 0.4 → 0.5").click()
    expect(page.get_by_label("Карточка изменения")).to_contain_text("Все основания недоступны")
    page.get_by_role("button").filter(has_text="Формат правки не распознан").click()
    expect(page.get_by_label("Карточка изменения")).to_contain_text("значения не показаны")


def test_decision_waits_for_the_operator_and_expiry_is_not_consent(page, live_panel):
    posted = open_development(
        page, live_panel, changes=[change("c-1", "AWAITING_APPROVAL", version=7)]
    )
    tab(page, "Изменения")
    page.get_by_role("button").filter(has_text="trust: 0.4 → 0.5").click()
    card = page.get_by_label("Карточка изменения")
    expect(card).to_contain_text("Истечение ожидания не означает одобрения")
    card.get_by_label("Основание решения (необязательно)").fill("Достаточно оснований")
    card.get_by_role("button", name="Принять", exact=True).click()
    expect(card.get_by_text("Команда принята ядром.")).to_be_visible()
    body = posted[0]
    assert body["type"] == "change.decision" and body["expected_version"] == 7
    assert body["arguments"]["data"] == {
        "change_id": "c-1",
        "decision": "accept",
        "reason": "Достаточно оснований",
    }


def test_a_version_conflict_on_decision_never_overwrites(page, live_panel):
    def command(request, body, count):
        request.fulfill(status=409, json={"code": "VERSION_CONFLICT", "message": "private"})

    posted = open_development(
        page, live_panel, changes=[change("c-1", "AWAITING_APPROVAL")], command=command
    )
    tab(page, "Изменения")
    page.get_by_role("button").filter(has_text="trust: 0.4 → 0.5").click()
    page.get_by_label("Карточка изменения").get_by_role(
        "button", name="Отклонить", exact=True
    ).click()
    expect(page.get_by_role("alert")).to_contain_text("уже изменена")
    assert len(posted) == 1 and "private" not in page.content()


def test_a_lost_reply_is_checked_with_the_same_decision(page, live_panel):
    attempts = []

    def command(request, body, count):
        attempts.append(body)
        if len(attempts) == 1:
            request.abort()
        else:
            request.fulfill(
                json={
                    "request_id": body["request_id"],
                    "scope": body["scope"],
                    "status": "completed",
                }
            )

    open_development(
        page, live_panel, changes=[change("c-1", "AWAITING_APPROVAL")], command=command
    )
    tab(page, "Изменения")
    page.get_by_role("button").filter(has_text="trust: 0.4 → 0.5").click()
    card = page.get_by_label("Карточка изменения")
    card.get_by_role("button", name="Принять", exact=True).click()
    expect(card.get_by_role("alert")).to_contain_text("Результат неизвестен")
    card.get_by_role("button", name="Проверить исход").click()
    expect(card.get_by_text("Команда принята ядром.")).to_be_visible()
    assert len(attempts) == 2 and attempts[0] == attempts[1]


def test_interpretation_correction_needs_a_new_text_and_a_reason(page, live_panel):
    posted = open_development(page, live_panel, changes=[change("c-1", "READY", version=4)])
    tab(page, "Изменения")
    page.get_by_role("button").filter(has_text="trust: 0.4 → 0.5").click()
    card = page.get_by_label("Карточка изменения")
    card.get_by_role("button", name="Исправить интерпретацию").click()
    card.get_by_role("button", name="Сохранить исправление").click()
    expect(card.get_by_role("alert")).to_contain_text("не отличается")
    card.get_by_label("Исправленная интерпретация").fill("Он помог лишь однажды")
    card.get_by_role("button", name="Сохранить исправление").click()
    expect(card.get_by_role("alert")).to_contain_text("основание")
    assert posted == []
    card.get_by_label("Основание исправления").fill("Второго раза не было")
    card.get_by_role("button", name="Сохранить исправление").click()
    expect(card.get_by_text("Команда принята ядром.")).to_be_visible()
    assert posted[0]["type"] == "change.interpretation" and posted[0]["expected_version"] == 4


def test_rollback_is_a_plan_then_one_command_with_a_compensating_version(page, live_panel):
    posted = open_development(
        page, live_panel, changes=[change("c-1", "APPLIED", version=5)], plan=plan_row()
    )
    tab(page, "Изменения")
    page.get_by_role("button").filter(has_text="trust: 0.4 → 0.5").click()
    card = page.get_by_label("Карточка изменения")
    card.get_by_role("button", name="Откатить", exact=True).click()
    card.get_by_role("button", name="Подготовить откат").click()
    plan = card.get_by_label("План последствий")
    expect(plan).to_contain_text("компенсирующая версия")
    expect(plan).to_contain_text("не отменяет событие мира")
    execute = plan.get_by_role("button", name="Откатить изменение")
    expect(execute).to_be_disabled()
    plan.get_by_label("Я понимаю", exact=False).check()
    execute.click()
    expect(card.get_by_label("Ход выполнения")).to_contain_text("создаём компенсирующую версию")
    assert [body["type"] for body in posted] == [
        "change.rollback.preview",
        "change.rollback.execute",
    ]
    assert posted[0]["expected_version"] == 5 and posted[0]["arguments"]["data"] == {
        "change_id": "c-1"
    }
    assert posted[1]["arguments"] == {"plan_id": "plan-1", "plan_version": 1}


@pytest.mark.parametrize(
    ("reason", "plan"),
    [
        (
            "не содержит выбранное изменение",
            plan_row(dependencies=[{"resource_id": "other", "version": 1}]),
        ),
        ("Срок плана истёк", plan_row(expires_at="2020-01-01T00:00:00Z")),
    ],
)
def test_a_rollback_plan_for_another_change_or_expired_cannot_run(page, live_panel, reason, plan):
    open_development(page, live_panel, changes=[change("c-1", "APPLIED")], plan=plan)
    tab(page, "Изменения")
    page.get_by_role("button").filter(has_text="trust: 0.4 → 0.5").click()
    card = page.get_by_label("Карточка изменения")
    card.get_by_role("button", name="Откатить", exact=True).click()
    card.get_by_role("button", name="Подготовить откат").click()
    plan_box = card.get_by_label("План последствий")
    expect(plan_box.get_by_role("alert")).to_contain_text(reason)
    plan_box.get_by_label("Я понимаю", exact=False).check()
    expect(plan_box.get_by_role("button", name="Откатить изменение")).to_be_disabled()


def test_a_rollback_itself_cannot_be_rolled_back(page, live_panel):
    open_development(
        page,
        live_panel,
        changes=[
            change(
                "c-1",
                "APPLIED",
                patch={
                    "schema_id": "development.patch",
                    "data": {"property": "trust", "before": 0.5, "after": 0.4, "kind": "rollback"},
                },
            )
        ],
    )
    tab(page, "Изменения")
    page.get_by_role("button").filter(has_text="trust: 0.5 → 0.4").click()
    card = page.get_by_label("Карточка изменения")
    expect(card).to_contain_text("Компенсирующая версия")
    expect(card.get_by_role("button", name="Откатить", exact=True)).to_have_count(0)


def test_bounds_show_the_profile_limits_state_and_the_missing_rule_editor(page, live_panel):
    open_development(
        page,
        live_panel,
        arcs=[arc()],
        opportunities=[opportunity("o-1", "BLOCKED", reason_code="граница-насилие")],
    )
    tab(page, "Границы")
    expect(page.get_by_label("Допустимые изменения")).to_contain_text("Профиль alpha · версия 1")
    page.get_by_text("Пределы развития").click()
    expect(page.get_by_label("Допустимые изменения")).to_contain_text('"step": 0.1')
    expect(page.get_by_label("Текущее состояние")).to_contain_text("Версия состояния 3")
    expect(page.get_by_label("Границы линий")).to_contain_text("Без насилия и принуждения")
    expect(page.get_by_label("Границы линий")).to_contain_text("Заблокировано: граница-насилие")
    expect(page.get_by_text("Редактор правил")).to_be_visible()


def test_empty_state_test_memory_and_narrow_window(page, live_panel):
    open_development(page, live_panel)
    expect(page.get_by_label("Требует внимания")).to_contain_text("Ничего не ждёт решения.")
    expect(page.get_by_label("Путь развития")).to_contain_text("пока нет")
    page.get_by_role("combobox", name="Просматриваемый персонаж и память", exact=True).click()
    page.get_by_role("option", name="beta · ТЕСТ · beta-test", exact=True).click()
    expect(page.get_by_text("Тестовая память", exact=True)).to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.get_by_role("button", name=STOP_BUTTON)).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
