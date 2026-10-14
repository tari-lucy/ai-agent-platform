"""M4-07 acceptance: workspace screen on deterministic projections (UI-only, no live backend)."""

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


def run_row(state="ACTIVE", activity="OPEN", unconfirmed=()):
    return {
        **base("run-alpha", 0, version=4),
        "scope": RUN_SCOPE,
        "agent_id": "primary",
        "run_id": "run-alpha",
        "session_id": "session-alpha",
        "control_epoch": 1,
        "state": state,
        "activity": activity,
        "autonomy": "observation",
        "voice_enabled": False,
        "gm_paused": True,
        "unconfirmed_resource_ids": list(unconfirmed),
    }


def operation(name, status, minute=30, **over):
    row = {**base(name, 2), "operation_id": name, "status": status, "deadline": FAR}
    row["deadline"] = stamp(minute) if minute else FAR
    row.update(over)
    return row


def capability(name, health="ready", effect="local_write"):
    return {
        **base(name),
        "adapter_id": "chat",
        "adapter_version": "1",
        "input_schema_id": "in",
        "output_schema_id": "out",
        "effect": effect,
        "required_permissions": [],
        "resource_ids": [],
        "timeout_ms": 1000,
        "cancellation_supported": True,
        "idempotency": "supported",
        "health": health,
        "setting_references": [],
    }


def connection(name, configured=True, verification="verified"):
    return {
        **base(name),
        "name": "Провайдер " + name,
        "adapter_id": "openai",
        "adapter_version": "1",
        "endpoint_setting": "AI_PLATFORM_URL",
        "credential_setting": "AI_PLATFORM_KEY",
        "configured": configured,
        "verification": verification,
        "capabilities": [],
    }


def permission(name, level="operator_decision", version=3, **over):
    row = {
        **base(name, version=version),
        "principal_id": "agent",
        "capability_id": "cap-1",
        "operation_type": "speak",
        "binding_id": "b1",
        "level": level,
        "conditions": {"schema_id": "c", "data": {}},
        "limits": {"schema_id": "l", "data": {}},
        "policy_version": 1,
        "author_id": "operator",
        "expires_at": FAR,
        "revoked_at": None,
    }
    row.update(over)
    return row


def page_of(items):
    return {"items": items, "limit": 50, "next_cursor": None, "complete": True}


def plan_row(**over):
    row = {
        **base("plan-1", 20),
        "operation_type": "workspace.restore",
        "consequences": "Текущее состояние будет заменено содержимым копии.",
        "dependencies": [{"resource_id": "bk-1", "version": 1}],
        "expires_at": FAR,
    }
    row.update(over)
    return row


def mock_workspace(
    page,
    *,
    run="ACTIVE",
    activity="OPEN",
    unconfirmed=(),
    operations=(),
    capabilities=(),
    connections=(),
    permissions=(),
    plan=None,
    command=None,
):
    posted = []

    def route(request):
        parts = urlsplit(request.request.url)
        query = parse_qs(parts.query)
        path = parts.path
        body = request.request.post_data_json if request.request.method == "POST" else None
        selected = query.get("persona_id", ["alpha"])[0]
        data = {
            "/api/v1/operations": operations,
            "/api/v1/capabilities": capabilities,
            "/api/v1/connections": connections,
            "/api/v1/permissions": permissions,
            "/api/v1/workspaces": [],
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
            row = run_row(run, activity, unconfirmed) if selected == "alpha" and run else None
            request.fulfill(json=page_of([row] if row else []))
        elif path in data:
            request.fulfill(json=page_of(list(data[path]) if selected == "alpha" else []))
        elif path == "/api/v1/plans/plan-1" and plan is not None:
            request.fulfill(json=plan)
        elif path == "/api/v1/operations/op-9":
            request.fulfill(
                json={
                    **base("op-9", 21),
                    "operation_id": "op-9",
                    "status": "RUNNING",
                    "deadline": FAR,
                    "waiting_reason": "замена файлов",
                }
            )
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
    return posted


def open_workspace(page, origin, **kwargs):
    posted = mock_workspace(page, **kwargs)
    page.goto(origin + "/#/workspace")
    expect(page.get_by_role("heading", name="Рабочее пространство", level=1)).to_be_visible()
    return posted


def test_indicators_are_separate_and_a_stopped_agent_needs_an_explicit_start(page, live_panel):
    open_workspace(page, live_panel, run="STOPPED")
    card = page.get_by_role("heading", name="Запуск в этой области").locator("xpath=..")
    for text in (
        "Запуск: Остановлен",
        "Деятельность: Деятельность разрешена",
        "Режим: Наблюдение",
        "Голос: Не подключён / выключен",
        "Гейммастер: На паузе",
    ):
        expect(card).to_contain_text(text)
    expect(card).to_contain_text("Возобновление требует отдельного действия")
    expect(page.get_by_role("button", name="Запустить", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="Пауза деятельности")).to_be_disabled()


def test_stopping_lists_unconfirmed_resources_and_closes_every_control(page, live_panel):
    open_workspace(page, live_panel, run="STOPPING", unconfirmed=["synthetic-worker"])
    expect(page.get_by_text("Остановка ещё не подтверждена")).to_be_visible()
    expect(page.get_by_text("synthetic-worker").first).to_be_visible()
    expect(page.get_by_text("Это не STOPPED.")).to_be_visible()
    for name in ("Запустить", "Пауза деятельности", "Возобновить деятельность", "Завершить сессию"):
        expect(page.get_by_role("button", name=name, exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name=STOP_BUTTON)).to_be_enabled()


def test_faulted_run_says_the_state_is_unknown(page, live_panel):
    open_workspace(page, live_panel, run="FAULTED")
    expect(page.get_by_text("запуск закрыт до сверки ресурсов")).to_be_visible()
    expect(page.get_by_role("button", name="Запустить", exact=True)).to_be_disabled()


def test_start_posts_the_seen_version_and_a_lost_reply_is_checked_with_the_same_command(
    page, live_panel
):
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

    open_workspace(page, live_panel, run="STOPPED", command=command)
    page.get_by_role("button", name="Запустить", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Результат неизвестен")
    assert len(attempts) == 1
    page.get_by_role("button", name="Проверить исход").click()
    expect(page.get_by_text("Команда принята ядром.")).to_be_visible()
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert attempts[0]["type"] == "agent.start" and attempts[0]["expected_version"] == 4
    assert (
        attempts[0]["arguments"] == {"autonomy": "observation"}
        and attempts[0]["target"] == "primary"
    )


def test_pause_is_offered_only_for_working_activity_and_resume_only_when_paused(page, live_panel):
    open_workspace(page, live_panel, run="ACTIVE", activity="PAUSED")
    expect(page.get_by_role("button", name="Возобновить деятельность")).to_be_enabled()
    expect(page.get_by_role("button", name="Пауза деятельности")).to_be_disabled()
    expect(page.get_by_role("button", name="Запустить", exact=True)).to_be_disabled()


def test_operations_put_doubtful_work_first_and_never_call_a_receipt_a_cancellation(
    page, live_panel
):
    posted = open_workspace(
        page,
        live_panel,
        operations=[
            operation("done", "SUCCEEDED", 5),
            operation("later", "RUNNING", 50),
            operation("doubt", "OUTCOME_UNKNOWN", 40),
            operation("soon", "QUEUED", 10),
        ],
    )
    items = page.get_by_role("list", name="Список заданий").get_by_role("listitem")
    expect(items).to_have_count(4)
    expect(items.nth(0)).to_contain_text("doubt")
    expect(items.nth(0)).to_contain_text("Требуется сверка с владельцем")
    expect(items.nth(3)).to_contain_text("done")
    expect(items.nth(0).get_by_role("button", name="Отменить")).to_have_count(0)
    expect(items.nth(3).get_by_role("button", name="Отменить")).to_have_count(0)
    expect(items.nth(1)).to_contain_text("просрочено")
    items.nth(1).get_by_role("button", name="Отменить").click()
    expect(page.get_by_text("Запрос на отмену принят")).to_be_visible()
    cancel = posted[0]
    assert cancel["type"] == "operation.cancel" and cancel["arguments"]["data"] == {
        "operation_id": "soon"
    }
    assert cancel["expected_version"] == 1


def test_degradation_is_explained_and_secrets_are_never_shown(page, live_panel):
    open_workspace(
        page,
        live_panel,
        capabilities=[
            capability("cap-1", "degraded"),
            capability("cap-2", "unavailable", "public_output"),
        ],
        connections=[connection("c1", configured=False), connection("c2", verification="failed")],
    )
    alert = page.get_by_text("Причины деградации").locator("xpath=../..")
    expect(alert).to_contain_text("Возможность cap-1: деградирует")
    expect(alert).to_contain_text("Подключение Провайдер c1: не настроено")
    expect(alert).to_contain_text("Подключение Провайдер c2: проверка не прошла")
    expect(page.get_by_text("Публичный выпуск")).to_be_visible()
    content = page.content()
    assert "AI_PLATFORM_KEY" not in content and "AI_PLATFORM_URL" not in content


def test_widening_a_right_needs_a_tick_and_the_seen_version(page, live_panel):
    def command(request, body, count):
        request.fulfill(
            status=409, json={"code": "VERSION_CONFLICT", "message": "private"}
        ) if count == 2 else request.fulfill(
            json={"request_id": body["request_id"], "scope": body["scope"], "status": "completed"}
        )

    posted = open_workspace(
        page,
        live_panel,
        capabilities=[capability("cap-1", effect="public_output")],
        permissions=[permission("perm-1")],
        command=command,
    )
    expect(page.get_by_role("list", name="Список прав")).to_contain_text("По решению оператора")
    page.get_by_role("button", name="Изменить", exact=True).click()
    form = page.get_by_label("Изменение права")
    form.get_by_label("Новый уровень").click()
    page.get_by_role("option", name="Автоматически", exact=True).click()
    expect(form.get_by_role("alert")).to_contain_text("без вашего решения")
    apply = form.get_by_role("button", name="Применить")
    expect(apply).to_be_disabled()
    form.get_by_label("Подтверждаю расширение прав").check()
    expect(apply).to_be_enabled()
    apply.click()
    expect(page.get_by_text("Изменение права принято.")).to_be_visible()
    assert posted[0]["type"] == "permission.edit" and posted[0]["expected_version"] == 3
    assert posted[0]["arguments"]["data"] == {"permission_id": "perm-1", "level": "automatic"}


def test_lowering_a_right_needs_no_extra_tick_and_a_conflict_never_overwrites(page, live_panel):
    def command(request, body, count):
        request.fulfill(status=409, json={"code": "VERSION_CONFLICT", "message": "private"})

    posted = open_workspace(page, live_panel, permissions=[permission("perm-1")], command=command)
    page.get_by_role("button", name="Изменить", exact=True).click()
    form = page.get_by_label("Изменение права")
    form.get_by_label("Новый уровень").click()
    page.get_by_role("option", name="Запрещено", exact=True).click()
    expect(form.get_by_label("Подтверждаю расширение прав")).to_have_count(0)
    form.get_by_role("button", name="Применить").click()
    expect(page.get_by_role("alert")).to_contain_text("уже изменено")
    assert len(posted) == 1 and "private" not in page.content()


def test_revoked_and_expired_rights_are_shown_as_such_and_cannot_be_edited(page, live_panel):
    open_workspace(
        page,
        live_panel,
        permissions=[
            permission("p1", revoked_at=stamp(5)),
            permission("p2", expires_at="2020-01-01T00:00:00Z"),
        ],
    )
    expect(page.get_by_text("Отозвано", exact=True)).to_be_visible()
    expect(page.get_by_text("Срок истёк", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Изменить", exact=True)).to_have_count(0)


def test_backup_is_a_plan_then_one_command(page, live_panel):
    posted = open_workspace(
        page, live_panel, plan=plan_row(operation_type="workspace.backup", dependencies=[])
    )
    page.get_by_role("button", name="Подготовить резервную копию").click()
    panel = page.get_by_label("План последствий")
    expect(panel).to_contain_text("не публикация")
    panel.get_by_role("button", name="Создать копию").click()
    expect(page.get_by_label("Ход выполнения")).to_be_visible()
    assert [body["type"] for body in posted] == [
        "workspace.backup.preview",
        "workspace.backup.execute",
    ]


def test_restore_is_closed_while_the_agent_runs(page, live_panel):
    open_workspace(page, live_panel, run="ACTIVE")
    page.get_by_label("Идентификатор копии").fill("bk-1")
    expect(page.get_by_text("только при остановленном агенте")).to_be_visible()
    expect(page.get_by_role("button", name="Подготовить восстановление")).to_be_disabled()


def test_restore_needs_the_copy_id_typed_back_and_leaves_the_agent_stopped(page, live_panel):
    posted = open_workspace(page, live_panel, run="STOPPED", plan=plan_row())
    page.get_by_label("Идентификатор копии").fill("bk-1")
    page.get_by_role("button", name="Подготовить восстановление").click()
    panel = page.get_by_label("План последствий")
    expect(panel).to_contain_text("Текущее состояние будет заменено")
    execute = panel.get_by_role("button", name="Восстановить", exact=True)
    expect(execute).to_be_disabled()
    panel.get_by_label("Агент остановлен, я понимаю", exact=False).check()
    expect(execute).to_be_disabled()
    panel.get_by_label("Введите «bk-1»", exact=False).fill("bk-2")
    expect(execute).to_be_disabled()
    panel.get_by_label("Введите «bk-1»", exact=False).fill("bk-1")
    expect(execute).to_be_enabled()
    assert [body["type"] for body in posted] == ["workspace.restore.preview"]
    execute.click()
    status = page.get_by_label("Ход выполнения")
    expect(status).to_contain_text("замена файлов")
    expect(status).to_contain_text("агент остаётся остановленным и требует явного запуска")
    assert posted[1]["type"] == "workspace.restore.execute"
    assert posted[1]["arguments"] == {"plan_id": "plan-1", "plan_version": 1}
    assert posted[0]["arguments"]["data"] == {"backup_id": "bk-1"}


@pytest.mark.parametrize(
    ("reason", "plan"),
    [
        (
            "не содержит выбранную копию",
            plan_row(dependencies=[{"resource_id": "other", "version": 1}]),
        ),
        ("Срок плана истёк", plan_row(expires_at="2020-01-01T00:00:00Z")),
    ],
)
def test_a_restore_plan_for_another_copy_or_expired_is_not_executable(
    page, live_panel, reason, plan
):
    open_workspace(page, live_panel, run="STOPPED", plan=plan)
    page.get_by_label("Идентификатор копии").fill("bk-1")
    page.get_by_role("button", name="Подготовить восстановление").click()
    panel = page.get_by_label("План последствий")
    expect(panel.get_by_role("alert")).to_contain_text(reason)
    panel.get_by_label("Агент остановлен, я понимаю", exact=False).check()
    panel.get_by_label("Введите «bk-1»", exact=False).fill("bk-1")
    expect(panel.get_by_role("button", name="Восстановить", exact=True)).to_be_disabled()


def test_changing_the_copy_id_discards_a_prepared_plan(page, live_panel):
    open_workspace(page, live_panel, run="STOPPED", plan=plan_row())
    box = page.get_by_label("Идентификатор копии")
    box.fill("bk-1")
    page.get_by_role("button", name="Подготовить восстановление").click()
    expect(page.get_by_label("План последствий")).to_be_visible()
    box.fill("bk-2")
    expect(page.get_by_label("План последствий")).to_have_count(0)


def test_test_memory_is_labelled_and_stop_stays_on_a_narrow_window(page, live_panel):
    open_workspace(page, live_panel)
    page.get_by_role("combobox", name="Просматриваемый персонаж и память", exact=True).click()
    page.get_by_role("option", name="beta · ТЕСТ · beta-test", exact=True).click()
    expect(page.get_by_text("Тестовая память", exact=True)).to_be_visible()
    expect(
        page.get_by_text("Нет подтверждённого запуска: операции с копиями недоступны.")
    ).to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.get_by_role("button", name=STOP_BUTTON)).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
