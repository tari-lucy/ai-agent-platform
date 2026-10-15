"""M4-03 acceptance: persona editor and people tab on deterministic projections (UI-only)."""

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
FAR = "2099-01-01T00:00:00Z"
STOP_BUTTON = "STOP — остановить активного агента"
HTML = "<script>window.fakeExecuted = true</script>"


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


def profile(version=3, canon="Строка один\nСтрока два", style="Мягкий стиль"):
    return {
        **base("profile-1", version=version),
        "persona_id": "alpha",
        "canon": canon,
        "style": style,
        "accepted_revision_id": "rev-1",
        "property_definitions": {"schema_id": "d", "data": {"trust": {"autonomy": "experience"}}},
        "initial_values": {"schema_id": "i", "data": {"trust": 0.4, "calm": "high"}},
        "development_bounds": {"schema_id": "b", "data": {"trust": {"step": 0.1}}},
    }


def state(profile_version=3):
    return {
        **base("state-1"),
        "persona_id": "alpha",
        "profile_version": profile_version,
        "state_version": 5,
        "values": {"schema_id": "v", "data": {"trust": 0.55}},
    }


def account(platform, ident, name, confirmation="confirmed"):
    return {
        "platform_id": platform,
        "account_id": ident,
        "display_name": name,
        "confirmation": confirmation,
        "evidence": [ref()],
    }


def person(ident, accounts, version=2):
    return {**base("row-" + ident, version=version), "person_id": ident, "accounts": accounts}


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


def page_of(items):
    return {"items": items, "limit": 50, "next_cursor": None, "complete": True}


def mock_screen(
    page,
    *,
    profiles=None,
    states=None,
    people=(),
    encounters=(),
    relationships=(),
    topics=(),
    promises=(),
    command=None,
):
    posted = []

    def route(request):
        parts = urlsplit(request.request.url)
        query = parse_qs(parts.query)
        path = parts.path
        body = request.request.post_data_json if request.request.method == "POST" else None
        mine = query.get("persona_id", ["alpha"])[0] == "alpha"
        data = {
            "/api/v1/personas": [profile()] if profiles is None else profiles,
            "/api/v1/character-states": [state()] if states is None else states,
            "/api/v1/people": people,
            "/api/v1/encounters": encounters,
            "/api/v1/relationships": relationships,
            "/api/v1/topics": topics,
            "/api/v1/promises": promises,
            "/api/v1/sources": (),
            "/api/v1/episodes": (),
            "/api/v1/claims": (),
            "/api/v1/summaries": (),
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
            request.fulfill(json=page_of([run_row()] if mine else []))
        elif path in data:
            request.fulfill(json=page_of(list(data[path]) if mine else []))
        elif path in ("/api/v1/commands", "/api/v1/personas/alpha/validate"):
            posted.append(body)
            if command is not None:
                command(request, body, len(posted))
            elif body["type"] == "persona.validate":
                request.fulfill(
                    json={
                        "request_id": body["request_id"],
                        "scope": body["scope"],
                        "ready": True,
                        "digest": "a" * 64,
                        "current_version": body["expected_version"],
                        "version_conflict": False,
                        "unchanged": False,
                        "issues": [],
                        "blockers": [],
                        "diff": {},
                    }
                )
            else:
                request.fulfill(
                    json={
                        "request_id": body["request_id"],
                        "scope": body["scope"],
                        "status": "completed",
                        "resource_id": "profile-1",
                        "version": 4,
                    }
                )
        else:
            request.fulfill(status=404)

    page.route("**/api/v1/**", route)
    page.route_web_socket("**/api/v1/events", lambda socket: socket.on_message(lambda _: None))
    return posted


def open_persona(page, origin, **kwargs):
    posted = mock_screen(page, **kwargs)
    page.goto(origin + "/#/persona")
    expect(page.get_by_role("heading", name="Персонаж", level=1)).to_be_visible()
    return posted


def open_people(page, origin, **kwargs):
    posted = mock_screen(page, **kwargs)
    page.goto(origin + "/#/memory")
    page.get_by_role("tab", name="Люди").click()
    return posted


def test_accepted_profile_keeps_canon_and_live_state_in_separate_columns(page, live_panel):
    open_persona(page, live_panel, profiles=[profile(canon=f"Канон {HTML}")])
    card = page.get_by_label("Принятый профиль")
    expect(card).to_contain_text("версия 3")
    expect(card).to_contain_text("Канон <script>window.fakeExecuted = true</script>")
    assert page.evaluate("window.fakeExecuted") is None
    table = page.get_by_role("table", name="Свойства личности")
    row = table.get_by_role("row").filter(has_text="trust")
    expect(row).to_contain_text("experience")
    expect(row.get_by_role("cell").nth(2)).to_have_text("0.4")
    expect(row.get_by_role("cell").nth(3)).to_contain_text("0.55")
    expect(row).to_contain_text("отличается от начального")
    expect(
        table.get_by_role("row").filter(has_text="calm").get_by_role("cell").nth(3)
    ).to_contain_text("нет данных")


def test_a_state_bound_to_another_profile_version_is_flagged(page, live_panel):
    open_persona(page, live_panel, states=[state(profile_version=2)])
    expect(page.get_by_label("Свойства", exact=True)).to_contain_text(
        "Состояние привязано к версии профиля v2"
    )


def test_no_profile_says_so_and_invents_no_memories(page, live_panel):
    open_persona(page, live_panel, profiles=[], states=[])
    expect(page.get_by_text("Профиля нет")).to_be_visible()
    expect(page.get_by_text("фиктивные воспоминания не создаются")).to_be_visible()
    expect(page.get_by_label("Новая личность")).to_be_visible()


def test_the_editor_shows_the_diff_and_closes_apply_until_the_draft_is_validated(page, live_panel):
    posted = open_persona(page, live_panel)
    editor = page.get_by_label("Редактор профиля")
    apply = editor.get_by_role("button", name="Применить версию")
    validate = editor.get_by_role("button", name="Проверить правку")
    expect(validate).to_be_disabled()
    expect(editor).to_contain_text("Изменений нет.")
    editor.get_by_label("Канон").fill("Строка один\nСтрока вторая, уточнённая")
    expect(editor.get_by_label("Различия: Канон")).to_contain_text("− Строка два")
    expect(editor.get_by_label("Различия: Канон")).to_contain_text("+ Строка вторая, уточнённая")
    expect(apply).to_be_disabled()
    expect(editor).to_contain_text("Сначала проверьте правку.")
    validate.click()
    expect(editor.get_by_text("Правка проверена.")).to_be_visible()
    expect(apply).to_be_enabled()
    assert posted[0]["type"] == "persona.validate" and posted[0]["expected_version"] == 3
    editor.get_by_label("Стиль").fill("Другой стиль")  # the checked draft changed
    expect(apply).to_be_disabled()
    expect(editor).to_contain_text("проверьте заново")


def test_apply_repeats_the_checked_digest_and_the_seen_version(page, live_panel):
    posted = open_persona(page, live_panel)
    editor = page.get_by_label("Редактор профиля")
    editor.get_by_label("Стиль").fill("Тёплый стиль")
    editor.get_by_role("button", name="Проверить правку").click()
    editor.get_by_role("button", name="Применить версию").click()
    expect(editor.get_by_text("Версия принята ядром.")).to_be_visible()
    apply = posted[1]
    assert apply["type"] == "persona.apply" and apply["expected_version"] == 3
    assert apply["arguments"]["data"]["revision_digest"] == "a" * 64
    assert "## Стиль\n\nТёплый стиль" in apply["arguments"]["data"]["markdown"]
    assert apply["target"] == "alpha"
    assert apply["scope"] == WORK


def test_without_a_returned_digest_nothing_is_applied(page, live_panel):
    def command(request, body, count):
        request.fulfill(
            json={
                "request_id": body["request_id"],
                "scope": body["scope"],
                "ready": False,
                "digest": None,
                "current_version": body["expected_version"],
                "version_conflict": False,
                "unchanged": False,
                "issues": [],
                "blockers": [],
                "diff": {},
            }
        )

    posted = open_persona(page, live_panel, command=command)
    editor = page.get_by_label("Редактор профиля")
    editor.get_by_label("Стиль").fill("Тёплый стиль")
    editor.get_by_role("button", name="Проверить правку").click()
    expect(editor).to_contain_text("Ядро не вернуло проверенную редакцию")
    expect(editor.get_by_role("button", name="Применить версию")).to_be_disabled()
    assert len(posted) == 1


def test_field_errors_sit_next_to_the_fields(page, live_panel):
    open_persona(page, live_panel)
    editor = page.get_by_label("Редактор профиля")
    editor.get_by_label("Канон").fill("")
    expect(editor).to_contain_text("Поле не может быть пустым.")
    expect(editor.get_by_role("button", name="Проверить правку")).to_be_disabled()


def test_a_version_conflict_offers_the_current_edition_and_never_overwrites(page, live_panel):
    def command(request, body, count):
        request.fulfill(status=409, json={"code": "VERSION_CONFLICT", "message": "private"})

    posted = open_persona(page, live_panel, command=command)
    editor = page.get_by_label("Редактор профиля")
    editor.get_by_label("Стиль").fill("Тёплый стиль")
    editor.get_by_role("button", name="Проверить правку").click()
    expect(editor.get_by_role("alert")).to_contain_text("Профиль уже изменён")
    expect(editor.get_by_role("button", name="Загрузить актуальную редакцию")).to_be_visible()
    assert len(posted) == 1 and "private" not in page.content()


def test_a_lost_reply_tells_to_check_the_version_and_does_not_retry(page, live_panel):
    attempts = []

    def command(request, body, count):
        attempts.append(body)
        request.abort()

    open_persona(page, live_panel, command=command)
    editor = page.get_by_label("Редактор профиля")
    editor.get_by_label("Стиль").fill("Тёплый стиль")
    editor.get_by_role("button", name="Проверить правку").click()
    expect(editor.get_by_role("alert")).to_contain_text("Результат неизвестен")
    assert len(attempts) == 1  # no automatic retry
    expect(editor.get_by_role("button", name="Загрузить актуальную редакцию")).to_be_visible()


def test_copy_never_carries_experience_and_needs_its_own_space(page, live_panel):
    posted = open_persona(page, live_panel)
    form = page.get_by_label("Новая личность")
    expect(form).to_contain_text("Память, состояние и отношения не копируются")
    form.get_by_role("button", name="Создать копию").click()
    expect(form.get_by_role("alert")).to_contain_text("Идентификатор личности")
    form.get_by_label("Идентификатор личности").fill("alpha")
    form.get_by_label("Область памяти новой личности").fill("copy-space")
    form.get_by_role("button", name="Создать копию").click()
    expect(form.get_by_role("alert")).to_contain_text("уже есть")
    form.get_by_label("Идентификатор личности").fill("beta-copy")
    form.get_by_role("button", name="Создать копию").click()
    expect(form.get_by_text("Новая личность не запускается автоматически.")).to_be_visible()
    assert posted[0]["type"] == "persona.copy"
    assert posted[0]["arguments"]["data"] == {
        "persona_id": "beta-copy",
        "memory_space_id": "copy-space",
        "source_profile_id": "profile-1",
    }


def test_create_from_scratch_needs_canon_and_style(page, live_panel):
    posted = open_persona(page, live_panel)
    form = page.get_by_label("Новая личность")
    form.get_by_text("С нуля").click()
    form.get_by_label("Идентификатор личности").fill("gamma")
    form.get_by_label("Область памяти новой личности").fill("gamma-work")
    form.get_by_role("button", name="Создать личность").click()
    expect(form.get_by_role("alert")).to_contain_text("нужны канон и стиль")
    assert posted == []
    form.get_by_label("Канон").fill("Вымышленный канон")
    form.get_by_label("Стиль").fill("Вымышленный стиль")
    form.get_by_role("button", name="Создать личность").click()
    expect(form.get_by_text("Команда принята ядром.")).to_be_visible()
    assert posted[0]["type"] == "persona.create"
    assert posted[0]["arguments"]["data"]["canon"] == "Вымышленный канон"


def test_people_with_the_same_nickname_stay_two_people(page, live_panel):
    open_people(
        page,
        live_panel,
        people=[
            person("p1", [account("chat", "a-1", "Ника")]),
            person("p2", [account("chat", "a-2", "ника")]),
            person("p3", [account("chat", "a-3", "Игорь")]),
        ],
    )
    people = page.get_by_role("list", name="Список людей")
    expect(people.get_by_role("listitem")).to_have_count(3)
    expect(people.get_by_role("listitem").filter(has_text="такой же ник")).to_have_count(2)
    people.get_by_role("button").filter(has_text="p1").click()
    card = page.get_by_label("Карточка человека")
    expect(card).to_contain_text("Это разные люди, пока аккаунты не подтверждены как один")
    expect(card).to_contain_text("p2")


def test_person_card_shows_confirmed_accounts_history_topics_and_promises(page, live_panel):
    open_people(
        page,
        live_panel,
        people=[
            person(
                "p1",
                [account("chat", "a-1", "Ника"), account("web", "w-1", "Гипотеза", "hypothesis")],
            )
        ],
        encounters=[
            {**base("m-1", 2), "person_ids": ["p1"], "sources": [ref()]},
            {**base("m-2", 9), "person_ids": ["p1", "p2"], "sources": [ref("deleted")]},
        ],
        relationships=[
            {
                **base("r-1"),
                "person_id": "p1",
                "aspects": {"schema_id": "a", "data": {"trust": 0.5}},
                "grounds": [ref(), ref("expired")],
            }
        ],
        topics=[
            {
                **base("t-1"),
                "topic_id": "topic-lake",
                "status": "answered",
                "participant_ids": ["p1"],
                "sources": [ref()],
                "answer": "Живёт у озера",
            }
        ],
        promises=[
            {
                **base("pr-1"),
                "participant_ids": ["p1"],
                "topic_id": "t-1",
                "delivered_utterance_id": "u-1",
                "delivery_id": "d-1",
                "sources": [ref()],
                "content": "Вернуться к вопросу",
                "completion_criterion": "Ответить",
                "status": "unknown",
                "closure_result": None,
            }
        ],
    )
    page.get_by_role("list", name="Список людей").get_by_role("button").first.click()
    card = page.get_by_label("Карточка человека")
    expect(card).to_contain_text("chat: a-1")
    expect(card).to_contain_text("Не подтверждены")
    expect(card).to_contain_text("web: w-1 · Гипотеза")
    expect(card).to_contain_text("История встреч (2)")
    expect(card).to_contain_text("1 основ., недоступно: 1")
    expect(card).to_contain_text("2 основ., недоступно: 1")
    expect(card).to_contain_text("Известный ответ: Живёт у озера")
    expect(card).to_contain_text("Исход неизвестен: удаление разговора не означает выполнения.")


def test_linking_needs_a_reason_a_tick_and_cannot_steal_a_confirmed_account(page, live_panel):
    posted = open_people(
        page,
        live_panel,
        people=[
            person("p1", [account("chat", "a-1", "Ника")], version=6),
            person("p2", [account("chat", "a-2", "Другой")]),
        ],
    )
    page.get_by_role("list", name="Список людей").get_by_role("button").filter(
        has_text="p1"
    ).click()
    form = page.get_by_label("Связать аккаунт")
    form.get_by_label("Платформа").fill("web")
    form.get_by_label("Идентификатор аккаунта").fill("w-9")
    form.get_by_role("button", name="Связать").click()
    expect(page.get_by_role("alert")).to_contain_text("основание")
    form.get_by_label("Основание: почему это тот же человек").fill("Сам сказал в чате")
    form.get_by_role("button", name="Связать").click()
    expect(page.get_by_role("alert")).to_contain_text("Подтвердите")
    form.get_by_label("Идентификатор аккаунта").fill("a-2")
    form.get_by_label("Платформа").fill("chat")
    form.get_by_label("Подтверждаю, что это один и тот же человек").check()
    form.get_by_role("button", name="Связать").click()
    expect(page.get_by_role("alert")).to_contain_text("уже подтверждён у другого человека")
    assert posted == []
    form.get_by_label("Платформа").fill("web")
    form.get_by_label("Идентификатор аккаунта").fill("w-9")
    form.get_by_role("button", name="Связать").click()
    expect(page.get_by_text("Команда принята ядром.")).to_be_visible()
    assert posted[0]["type"] == "person.link" and posted[0]["expected_version"] == 6
    assert posted[0]["arguments"]["data"] == {
        "person_id": "p1",
        "platform_id": "web",
        "account_id": "w-9",
        "reason": "Сам сказал в чате",
    }


def test_unlinking_needs_a_reason(page, live_panel):
    posted = open_people(
        page, live_panel, people=[person("p1", [account("chat", "a-1", "Ника")], version=3)]
    )
    page.get_by_role("list", name="Список людей").get_by_role("button").first.click()
    page.get_by_role("button", name="Отвязать", exact=True).first.click()
    box = page.get_by_label("Отвязка аккаунта")
    box.get_by_role("button", name="Отвязать").click()
    expect(page.get_by_role("alert")).to_contain_text("Укажите основание")
    assert posted == []
    box.get_by_label("Основание отвязки").fill("Связь была ошибочной")
    box.get_by_role("button", name="Отвязать").click()
    expect(page.get_by_text("Команда принята ядром.")).to_be_visible()
    assert posted[0]["type"] == "person.unlink" and posted[0]["expected_version"] == 3


def test_a_conflict_on_linking_never_overwrites(page, live_panel):
    def command(request, body, count):
        request.fulfill(status=409, json={"code": "VERSION_CONFLICT", "message": "private"})

    posted = open_people(
        page, live_panel, people=[person("p1", [account("chat", "a-1", "Ника")])], command=command
    )
    page.get_by_role("list", name="Список людей").get_by_role("button").first.click()
    form = page.get_by_label("Связать аккаунт")
    form.get_by_label("Платформа").fill("web")
    form.get_by_label("Идентификатор аккаунта").fill("w-9")
    form.get_by_label("Основание: почему это тот же человек").fill("Сам сказал")
    form.get_by_label("Подтверждаю, что это один и тот же человек").check()
    form.get_by_role("button", name="Связать").click()
    expect(page.get_by_role("alert")).to_contain_text("уже изменена")
    assert len(posted) == 1 and "private" not in page.content()


def test_test_memory_is_labelled_and_the_window_can_be_narrow(page, live_panel):
    open_persona(page, live_panel)
    page.get_by_role("combobox", name="Просматриваемый персонаж и память", exact=True).click()
    page.get_by_role("option", name="beta · ТЕСТ · beta-test", exact=True).click()
    expect(page.get_by_text("Тестовая память", exact=True)).to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.get_by_role("button", name=STOP_BUTTON)).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
