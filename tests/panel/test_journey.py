"""M5-03: one operator journey through the panel (real entry/STOP, mocked projections).

Entry (real login) -> dialogue -> restart (page reload) -> re-meeting -> development -> deletion
-> STOP (real STOP) -> exit. Projections and business commands are deterministic mocks: the core
has no business routes yet. No external host is contacted, nothing is stored in the browser, and
no secret reaches the DOM.
"""

import json
from urllib.parse import urlsplit

from playwright.sync_api import expect

SCOPE = {
    "workspace_id": "synthetic",
    "persona_id": "alpha",
    "memory_space_id": "test-bootstrap",
    "memory_mode": "test",
}
FAR = "2099-01-01T00:00:00Z"
BIRD = "зимородка-4417"
HTML = "<script>window.fakeExecuted = true</script>"


def stamp(minute):
    return f"2026-10-14T00:{minute:02d}:00Z"


def base(name, minute=1, version=1):
    return {
        "id": name,
        "scope": SCOPE,
        "schema_version": "1.0",
        "version": version,
        "created_at": stamp(minute),
        "updated_at": stamp(minute),
    }


def ref(source="s-bird"):
    return {"source_id": source, "version": 1, "scope": SCOPE, "availability": "available"}


def page_of(items):
    return {"items": items, "limit": 50, "next_cursor": None, "complete": True}


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
        "author_id": "nika",
        "content": text,
        "availability": "available",
        "retention": {"policy_id": "conversation-30d", "expires_at": FAR, "protected": False},
        "read_allowed": True,
        "api_transfer_allowed": True,
        "public_disclosure_allowed": False,
    }
    row.update(over)
    return row


class World:
    """The mocked backend: it changes only when a recorded command says so."""

    def __init__(self):
        self.commands = []
        self.sent = False
        self.decided = False
        self.requests = []


def install(page, world):
    def projection(path, run_page):
        if path == "/api/v1/sources":
            rows = [source("s-bird", f"Ника рассказала про редкую птицу {BIRD}. {HTML}", 2)]
            if world.sent:
                rows.append(
                    source(
                        "s-hello",
                        "Привет, это я",
                        5,
                        provenance="operator_input",
                        author_id="operator",
                    )
                )
            return rows
        if path == "/api/v1/utterances" and world.sent:
            return [
                {
                    **base("u-1", 6),
                    "utterance_id": "u-1",
                    "revision": 1,
                    "content": "Привет! Рада тебя видеть.",
                    "content_hash": "h" * 64,
                    "audience_id": "operator",
                    "channel_ids": ["panel"],
                    "moderation_status": "approved",
                    "moderation_check_id": "check-1",
                }
            ]
        if path == "/api/v1/deliveries" and world.sent:
            return [
                {
                    **base("d-1", 7),
                    "utterance_id": "u-1",
                    "revision": 1,
                    "channel_id": "panel",
                    "attempt_id": "a-1",
                    "status": "DELIVERED",
                }
            ]
        if path == "/api/v1/people":
            return [
                {
                    **base("row-nika", 2, 2),
                    "person_id": "nika",
                    "accounts": [
                        {
                            "platform_id": "chat",
                            "account_id": "acct-nika",
                            "display_name": "Ника",
                            "confirmation": "confirmed",
                            "evidence": [ref()],
                        }
                    ],
                }
            ]
        if path == "/api/v1/encounters":
            return [
                {**base("m-1", 2), "person_ids": ["nika"], "sources": [ref()]},
                {**base("m-2", 8), "person_ids": ["nika"], "sources": [ref()]},
            ]
        if path == "/api/v1/arcs":
            return [
                {
                    **base("arc-1"),
                    "direction": "Найти причину страха воды",
                    "boundaries": "Без насилия",
                    "status": "open",
                    "grounds": [ref()],
                    "episode_ids": [],
                    "cooldown_until": None,
                    "rules_version": 1,
                }
            ]
        if path == "/api/v1/changes":
            return [
                {
                    **base("c-1", 4, 2 if world.decided else 1),
                    "proposal_id": "p-1",
                    "owner": "persona",
                    "base_version": 3,
                    "new_version": 4 if world.decided else None,
                    "patch": {
                        "schema_id": "development.patch",
                        "data": {
                            "property": "humor",
                            "before": 0.5,
                            "after": 0.62,
                            "kind": "change",
                        },
                    },
                    "evidence": [ref()],
                    "interpretation": "Шутки сближают",
                    "uncertainty": 0.3,
                    "expected_manifestation": "Чаще шутит",
                    "status": "APPLIED" if world.decided else "AWAITING_APPROVAL",
                    "checks": [],
                }
            ]
        if path == "/api/v1/personas":
            return [
                {
                    **base("profile-1", 1, 3),
                    "persona_id": "alpha",
                    "canon": "Канон",
                    "style": "Стиль",
                    "accepted_revision_id": "rev-1",
                    "initial_values": {"schema_id": "i", "data": {}},
                    "property_definitions": {"schema_id": "d", "data": {}},
                    "development_bounds": {"schema_id": "b", "data": {}},
                }
            ]
        return []

    def route(request):
        parts = urlsplit(request.request.url)
        path, method = parts.path, request.request.method
        if path.startswith("/api/v1/auth/") or path.endswith("/stop") and "/agents/" in path:
            request.continue_()  # real core: login, session restore, logout, STOP
            return
        if path == "/api/v1/sessions":
            real = request.fetch()
            body = real.json()
            for row in body["items"]:
                row["state"] = (
                    "ACTIVE"  # only the state is shown as working; ids and versions are the core's
                )
            request.fulfill(response=real, json=body)
            return
        if method == "POST" and path == "/api/v1/commands":
            body = request.request.post_data_json
            world.commands.append(body)
            kind = body["type"]
            world.sent = world.sent or kind == "dialogue.message"
            world.decided = world.decided or kind == "change.decision"
            answer = {
                "request_id": body["request_id"],
                "scope": body["scope"],
                "status": "completed",
            }
            if kind.endswith(".preview"):
                answer["resource_id"] = "plan-1"
            elif kind.endswith(".execute"):
                answer.update(status="waiting", operation_id="op-9")
            request.fulfill(json=answer)
        elif path == "/api/v1/plans/plan-1":
            request.fulfill(
                json={
                    **base("plan-1", 20),
                    "operation_type": "memory.delete",
                    "consequences": "Будут удалены текст источника, выдержки и индексы.",
                    "dependencies": [{"resource_id": "s-bird", "version": 1}],
                    "expires_at": FAR,
                }
            )
        elif path == "/api/v1/operations/op-9":
            request.fulfill(
                json={
                    **base("op-9", 21),
                    "operation_id": "op-9",
                    "status": "SUCCEEDED",
                    "deadline": FAR,
                }
            )
        elif path.startswith("/api/v1/") and method == "GET":
            request.fulfill(json=page_of(projection(path, None)))
        else:
            request.fulfill(status=404)

    page.route("**/api/v1/**", route)
    page.route_web_socket("**/api/v1/events", lambda socket: socket.on_message(lambda _: None))


def test_the_whole_operator_journey(page, live_panel, panel_password):
    world = World()
    page.on("request", lambda r: world.requests.append((r.method, r.url, r.post_data or "")))
    console = []
    page.on("console", lambda m: console.append(m.text) if m.type == "error" else None)

    # ---- entry: a real login; entering never starts the agent ----
    page.goto(live_panel)
    page.get_by_label("Пароль", exact=True).fill(panel_password)
    page.get_by_role("button", name="Войти", exact=True).click()
    stop = page.get_by_role("button", name="STOP — остановить активного агента")
    expect(stop).to_be_enabled()
    expect(page.get_by_text("Остановлен", exact=False).first).to_be_visible()
    install(page, world)  # from here the core's business projections are deterministic mocks
    page.reload()
    expect(stop).to_be_enabled()

    # ---- dialogue: one message, one command ----
    page.get_by_role("link", name="Диалог", exact=True).click()
    page.get_by_label("Сообщение персонажу").fill("Привет, это я")
    page.get_by_role("button", name="Отправить", exact=True).click()
    expect(page.get_by_text("Сообщение принято и сохранено ядром.")).to_be_visible()
    assert [c["type"] for c in world.commands] == ["dialogue.message"]

    # ---- restart: the page is reloaded; the session returns, no command is replayed ----
    page.reload()
    expect(stop).to_be_enabled()
    timeline = page.get_by_role("list", name="Лента диалога")
    expect(timeline).to_contain_text("Привет, это я")
    expect(timeline).to_contain_text("Привет! Рада тебя видеть.")
    expect(timeline).to_contain_text("Завершено")
    assert [c["type"] for c in world.commands] == ["dialogue.message"], (
        "a reload must not repeat a command"
    )

    # ---- re-meeting: the person, the history, the plain text of what they said ----
    page.get_by_role("link", name="Память и люди", exact=True).click()
    page.get_by_role("tab", name="Люди").click()
    page.get_by_role("list", name="Список людей").get_by_role("button").first.click()
    card = page.get_by_label("Карточка человека")
    expect(card).to_contain_text("chat: acct-nika")
    expect(card).to_contain_text("История встреч (2)")

    # ---- development: the operator decides; the change becomes applied ----
    page.get_by_role("link", name="Сюжет и развитие", exact=True).click()
    expect(page.get_by_label("Требует внимания")).to_contain_text("Решение по изменению humor")
    page.get_by_role("tab", name="Изменения", exact=True).click()
    page.get_by_role("button").filter(has_text="humor: 0.5 → 0.62").click()
    change = page.get_by_label("Карточка изменения")
    expect(change).to_contain_text("Ожидает оператора")
    change.get_by_role("button", name="Принять", exact=True).click()
    expect(change.get_by_text("Команда принята ядром.")).to_be_visible()
    assert (
        world.commands[-1]["type"] == "change.decision"
        and world.commands[-1]["expected_version"] == 1
    )
    page.get_by_role("tab", name="Обзор", exact=True).click()
    expect(page.get_by_label("Путь развития")).to_contain_text("humor: 0.5 → 0.62")

    # ---- deletion: a plan first, an explicit tick, one command ----
    page.get_by_role("link", name="Память и люди", exact=True).click()
    page.get_by_role("tab", name="Записи").click()
    page.get_by_role("button").filter(has_text=BIRD).first.click()
    assert page.evaluate("window.fakeExecuted") is None  # the text is data, never markup
    page.get_by_role("button", name="Удалить", exact=True).first.click()
    page.get_by_role("button", name="Подготовить удаление").click()
    plan = page.get_by_label("План последствий")
    execute = plan.get_by_role("button", name="Удалить", exact=True)
    expect(execute).to_be_disabled()
    plan.get_by_label("Я понимаю, что удалённое восстановить нельзя").check()
    execute.click()
    expect(page.get_by_label("Ход выполнения")).to_contain_text("Выполнено: Удаление")
    assert [c["type"] for c in world.commands][-2:] == [
        "memory.delete.preview",
        "memory.delete.execute",
    ]

    # ---- STOP: the real core stops the real agent ----
    stop.click()
    expect(page.get_by_text("Ядро подтвердило остановку.", exact=False)).to_be_visible()

    # ---- exit: the session ends, the cookie goes ----
    page.get_by_role("button", name="Выйти", exact=True).click()
    expect(page.get_by_role("heading", name="Вход оператора")).to_be_visible()
    assert not any(c["name"] == "ai_operator_session" for c in page.context.cookies())

    # ---- invariants of the whole journey ----
    origin = live_panel.rstrip("/")
    assert all(url.startswith(origin) for _, url, _ in world.requests), "a request left the origin"
    assert all(
        urlsplit(url).path.startswith(("/api/v1/", "/assets/", "/index.html"))
        or urlsplit(url).path in ("/", "/favicon.ico")
        for _, url, _ in world.requests
    )
    posted = [
        json.loads(body)
        for method, url, body in world.requests
        if method == "POST" and body and "/api/v1/commands" in url
    ]
    assert [c["type"] for c in posted] == [c["type"] for c in world.commands]
    assert not page.evaluate("localStorage.length + sessionStorage.length")
    content = page.content()
    assert panel_password not in content and "AI_PLATFORM_" not in content
    assert panel_password not in " ".join(
        body for _, _, body in world.requests if "/auth/login" not in _
    )
    assert not [m for m in console if "fakeExecuted" in m]
