"""M4-06 acceptance: laboratory screen on deterministic projections (UI-only)."""

from urllib.parse import parse_qs, urlsplit

from ai_agent_application.laboratory.catalogue import catalogue
from playwright.sync_api import expect

WORK = {
    "workspace_id": "synthetic",
    "persona_id": "alpha",
    "memory_space_id": "alpha-work",
    "memory_mode": "work",
}
TEST = {**WORK, "persona_id": "beta", "memory_space_id": "beta-test", "memory_mode": "test"}
FAR = "2099-01-01T00:00:00Z"
STOP_BUTTON = "STOP — остановить активного агента"
REAL = "РЕАЛЬНЫЙ API"


def stamp(minute):
    return f"2026-10-14T00:{minute:02d}:00Z"


def summary(verdict="passed", checks=9, failed=0, seed="lab-seed", digest="abc123def456"):
    return f"MOCK {verdict}: {checks} checks, {failed} failed; seed {seed}; digest {digest}"


def report(name, scenario="api-cutoff", status="passed", minute=2, version=1, **over):
    row = {
        "id": name,
        "scope": TEST,
        "schema_version": "1.0",
        "version": version,
        "created_at": stamp(minute),
        "updated_at": stamp(minute),
        "scenario_id": scenario,
        "scenario_version": 1,
        "operation_id": "lab-" + name,
        "status": status,
        "report_artifact": None,
        "summary": summary(),
    }
    row.update(over)
    return row


def page_of(items):
    return {"items": items, "limit": 50, "next_cursor": None, "complete": True}


def mock_lab(page, *, reports=(), runs=(), command=None):
    posted = []

    def route(request):
        parts = urlsplit(request.request.url)
        query = parse_qs(parts.query)
        path = parts.path
        body = request.request.post_data_json if request.request.method == "POST" else None
        selected = query.get("persona_id", ["alpha"])[0]
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
            selected_scope = query.get("memory_mode", ["work"])[0]
            run_scope = {
                **WORK,
                "agent_id": "primary",
                "run_id": "run-1",
                "session_id": "session-1",
            }
            row = {
                "id": "run-1",
                "scope": run_scope,
                "version": 17,
                "created_at": stamp(0),
                "updated_at": stamp(0),
                "agent_id": "primary",
                "run_id": "run-1",
                "session_id": "session-1",
                "control_epoch": 1,
                "state": "ACTIVE",
                "activity": "OPEN",
                "autonomy": "observation",
                "voice_enabled": False,
                "gm_paused": True,
                "unconfirmed_resource_ids": [],
            }
            request.fulfill(json=page_of([row] if selected_scope == "work" else []))
        elif path == "/api/v1/laboratory-catalogue":
            request.fulfill(
                json={
                    "scope": TEST if selected == "beta" else WORK,
                    "mode": "mock",
                    "scenarios": [
                        {
                            "scenario_id": s.scenario_id,
                            "version": s.version,
                            "title": s.title,
                            "tags": list(s.tags),
                        }
                        for s in catalogue()
                    ],
                }
            )
        elif path == "/api/v1/laboratory-runs":
            request.fulfill(json={"scope": WORK, **page_of(list(runs))})
        elif path == "/api/v1/laboratory-reports":
            request.fulfill(json=page_of(list(reports) if selected == "beta" else []))
        elif path == "/api/v1/commands":
            posted.append(body)
            if command is not None:
                command(request, body, len(posted))
            else:
                request.fulfill(
                    json={
                        "request_id": body["request_id"],
                        "scope": body["scope"],
                        "status": "accepted",
                    }
                )
        else:
            request.fulfill(status=404)

    page.route("**/api/v1/**", route)
    page.route_web_socket("**/api/v1/events", lambda socket: socket.on_message(lambda _: None))
    return posted


def open_lab(page, origin, test_scope=True, **kwargs):
    posted = mock_lab(page, **kwargs)
    page.goto(origin + "/#/laboratory")
    expect(page.get_by_role("heading", name="Лаборатория", level=1)).to_be_visible()
    if test_scope:
        page.get_by_role("combobox", name="Просматриваемый персонаж и память", exact=True).click()
        page.get_by_role("option", name="beta · ТЕСТ · beta-test", exact=True).click()
        expect(page.get_by_label("Каталог сценариев")).to_be_visible()
    return posted


def pick(page, title):
    row = page.get_by_label("Каталог сценариев").get_by_role("listitem").filter(has_text=title)
    row.get_by_role("button", name="Выбрать").click()


def test_mock_green_is_never_presented_as_quality(page, live_panel):
    open_lab(page, live_panel)
    note = page.get_by_role("note")
    expect(note).to_contain_text("не доказывает качество личности или LLM")
    expect(note).to_contain_text("платные")


def test_work_memory_closes_the_laboratory_and_offers_an_isolated_area(page, live_panel):
    posted = open_lab(page, live_panel, test_scope=False)
    expect(page.get_by_text("Тестовая область не выбрана")).to_be_visible()
    expect(page.get_by_label("Каталог сценариев")).to_have_count(0)
    expect(page.get_by_label("Запуск сценария")).to_have_count(0)
    page.get_by_role("button", name="Создать изолированную тестовую область").click()
    expect(page.get_by_text("Тестовая область подготовлена", exact=False)).to_be_visible()
    assert posted[0]["type"] == "laboratory.create" and posted[0]["scope"]["memory_mode"] == "work"
    assert posted[0]["arguments"]["data"] == {"persona_id": "alpha"}


def test_catalogue_lists_all_scenarios_with_their_last_status(page, live_panel):
    open_lab(
        page,
        live_panel,
        reports=[
            report("r1", "api-cutoff", "passed"),
            report("r2", "same-nickname", "failed", 3),
            report("r3", "stop-during-generation", "unknown", 4),
        ],
    )
    rows = page.get_by_label("Каталог сценариев").get_by_role("listitem")
    expect(rows).to_have_count(12)
    expect(rows.filter(has_text="Обрыв API")).to_contain_text("Пройден")
    expect(rows.filter(has_text="Два человека с одинаковым ником")).to_contain_text("Не пройден")
    expect(rows.filter(has_text="STOP во время генерации")).to_contain_text("Исход неизвестен")
    expect(rows.filter(has_text="Удаление источника")).to_contain_text("Не запускался")


def test_mock_start_is_the_default_and_sends_seed_and_no_real_call(page, live_panel):
    posted = open_lab(page, live_panel, reports=[report("r1", "api-cutoff", version=4)])
    launch = page.get_by_label("Запуск сценария")
    launch.get_by_role("button", name="Запустить").click()
    expect(launch.get_by_role("alert")).to_contain_text("каталога")
    assert posted == []
    pick(page, "Обрыв API")
    launch.get_by_label("Seed (фиксируется в отчёте)").fill("seed-77")
    expect(launch).to_contain_text("детерминированный mock: без сети, без расхода")
    launch.get_by_role("button", name="Запустить").click()
    expect(launch.get_by_text("Команда принята ядром.")).to_be_visible()
    body = posted[0]
    assert (
        body["type"] == "laboratory.start"
        and body["scope"]["run_id"] == "run-1"
        and body["expected_version"] == 17
    )
    assert body["arguments"]["data"] == {
        "scenario_id": "api-cutoff",
        "scenario_version": 1,
        "seed": "seed-77",
        "mode": "mock",
        "lab_scope": TEST,
    }


def test_live_panel_does_not_offer_an_unconnected_paid_api(page, live_panel):
    posted = open_lab(page, live_panel)
    launch = page.get_by_label("Запуск сценария")
    expect(launch).to_contain_text("Реальные API здесь не подключены")
    expect(launch.get_by_label("Использовать реальный API (платно)")).to_have_count(0)
    assert posted == []


def test_catalogue_failure_closes_start_without_a_local_fallback(page, live_panel):
    posted = open_lab(page, live_panel)
    page.route(
        "**/api/v1/laboratory-catalogue?**",
        lambda route: route.fulfill(status=503, json={"code": "CAPABILITY_UNAVAILABLE"}),
    )
    page.reload()
    page.get_by_role("combobox", name="Просматриваемый персонаж и память", exact=True).click()
    page.get_by_role("option", name="beta · ТЕСТ · beta-test", exact=True).click()
    expect(page.get_by_text("Каталог недоступен", exact=True)).to_be_visible()
    launch = page.get_by_label("Запуск сценария")
    launch.get_by_role("button", name="Запустить").click()
    expect(launch.get_by_role("alert")).to_contain_text("каталога")
    assert posted == []


def test_a_lost_reply_is_checked_with_the_same_start_command(page, live_panel):
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
                    "status": "accepted",
                }
            )

    open_lab(page, live_panel, command=command)
    pick(page, "Обрыв API")
    launch = page.get_by_label("Запуск сценария")
    launch.get_by_role("button", name="Запустить").click()
    expect(launch.get_by_role("alert")).to_contain_text("Результат неизвестен")
    assert len(attempts) == 1
    launch.get_by_role("button", name="Проверить исход").click()
    expect(launch.get_by_text("Команда принята ядром.")).to_be_visible()
    assert len(attempts) == 2 and attempts[0] == attempts[1]


def job():
    return {
        "scope": {**WORK, "agent_id": "primary", "run_id": "run-1", "session_id": "session-1"},
        "lab_scope": TEST,
        "operation_id": "job-1",
        "version": 8,
        "scenario_id": "api-cutoff",
        "scenario_version": 1,
        "status": "RUNNING",
        "ready": True,
        "virtual_time": stamp(0),
        "accepted_events": 0,
    }


def test_only_one_job_at_a_time_and_stop_uses_job_cas(page, live_panel):
    posted = open_lab(
        page, live_panel, runs=[job()], reports=[report("r1", "api-cutoff", "running", version=3)]
    )
    pick(page, "Обрыв API")
    launch = page.get_by_label("Запуск сценария")
    expect(launch.get_by_role("button", name="Запустить")).to_be_disabled()
    expect(launch).to_contain_text("Идёт запуск")
    box = page.get_by_label("Лабораторное задание", exact=True)
    box.get_by_role("button", name="Остановить задание").click()
    expect(box.get_by_text("Отмена запрошена.", exact=False)).to_be_visible()
    assert posted[0]["type"] == "laboratory.stop" and posted[0]["expected_version"] == 8
    assert posted[0]["arguments"]["data"] == {"operation_id": "job-1", "lab_scope": TEST}
    assert posted[0]["scope"]["run_id"] == "run-1"


def test_reports_show_the_parsed_summary_and_never_a_passed_unknown(page, live_panel):
    open_lab(
        page,
        live_panel,
        reports=[
            report("r1", summary=summary("passed", 9, 0, "s1", "aaaaaaaaaaaa")),
            report("r2", "same-nickname", "unknown", 3, summary="что-то непонятное"),
        ],
    )
    reports = page.get_by_label("Отчёты")
    expect(reports).to_contain_text(
        "Режим: mock · проверок 9, не прошло 0 · seed s1 · отпечаток aaaaaaaaaaaa"
    )
    expect(reports).to_contain_text("Сводка не распознана; режим не определён.")
    expect(reports).to_contain_text("результат не засчитан как пройденный")


def test_replay_comparison_checks_the_digest_for_the_same_seed(page, live_panel):
    open_lab(
        page,
        live_panel,
        reports=[
            report("r1", minute=2),
            report("r2", minute=3),
            report("r3", minute=4, summary=summary(digest="ffffffffffff")),
            report("r4", minute=5, summary=summary(seed="other")),
        ],
    )
    boxes = page.get_by_label("Отчёты").get_by_label("Сравнить")
    boxes.nth(3).check()
    boxes.nth(2).check()
    expect(page.get_by_role("status", name="Сравнение отчётов")).to_contain_text(
        "Повтор воспроизводится"
    )
    boxes.nth(2).uncheck()
    boxes.nth(1).check()
    expect(page.get_by_role("status", name="Сравнение отчётов")).to_contain_text(
        "Отпечатки различаются"
    )
    boxes.nth(1).uncheck()
    boxes.nth(0).check()
    expect(page.get_by_role("status", name="Сравнение отчётов")).to_contain_text("Разные seed")


def test_events_are_bound_to_ready_job_and_test_scope(page, live_panel):
    posted = open_lab(page, live_panel, runs=[job()])
    box = page.get_by_label("Лабораторное задание", exact=True)
    expect(box.get_by_role("button", name="Подать событие")).to_be_disabled()
    assert posted == []
    box.get_by_label("Текст события").fill("Вымышленное наблюдение")
    box.get_by_role("button", name="Подать событие").click()
    expect(box.get_by_text("Наблюдение сохранено в тестовой памяти.", exact=True)).to_be_visible()
    assert posted[0]["type"] == "laboratory.event" and posted[0]["expected_version"] == 8
    assert posted[0]["arguments"]["data"] == {
        "operation_id": "job-1",
        "lab_scope": TEST,
        "text": "Вымышленное наблюдение",
    }
    assert posted[0]["scope"]["run_id"] == "run-1"


def test_settings_transfer_is_closed_until_the_core_gives_a_preview(page, live_panel):
    posted = open_lab(page, live_panel)
    box = page.get_by_label("Перенос настроек")
    expect(box.get_by_role("button", name="Перенести настройки")).to_be_disabled()
    expect(box).to_contain_text("Никогда не переносятся")
    expect(box).to_contain_text("Эпизоды")
    expect(box).to_contain_text("предпросмотр различий и блокировки совместимости")
    assert posted == []


def test_error_state_and_narrow_window(page, live_panel):
    open_lab(page, live_panel)
    expect(page.get_by_label("Отчёты")).to_contain_text("Отчётов пока нет.")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.get_by_role("button", name=STOP_BUTTON)).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
