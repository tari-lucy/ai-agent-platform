"""One browser path for guidance and memory receipts. Synthetic projections, no LLM."""

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
FAR = "2099-01-01T00:00:00Z"


def record(identifier, at="2026-10-15T09:00:00Z"):
    return dict(
        id=identifier, scope=WORK, schema_version="1.0", version=1, created_at=at, updated_at=at
    )


def test_guidance_memory_outcomes_and_read_failure(page, live_panel):
    posts, errors = [], []
    state = {"failed": False}
    page.on("pageerror", lambda error: errors.append(str(error)))
    run_scope = {**WORK, "agent_id": "primary", "run_id": "run-a", "session_id": "session-a"}
    run = {
        **record("run-a"),
        "scope": run_scope,
        "agent_id": "primary",
        "run_id": "run-a",
        "session_id": "session-a",
        "state": "STOPPED",
        "activity": "OPEN",
        "autonomy": "autonomous",
        "control_epoch": 1,
        "voice_enabled": False,
        "gm_paused": True,
        "unconfirmed_resource_ids": [],
    }
    ref = {"source_id": "s-a", "version": 1, "scope": WORK, "availability": "available"}
    claim = {
        **record("claim-a"),
        "subject_id": "synthetic",
        "content": "Вымышленная деталь разговора",
        "knowledge_type": "reported",
        "sources": [ref],
        "confidence": 0.5,
        "conflict_ids": [],
    }
    summary = {
        **record("summary-a"),
        "analysis_id": "analysis-a",
        "content": "Вымышленная сводка",
        "episode_ids": ["episode-a"],
        "sources": [ref],
        "period_start": "2026-10-14T00:00:00Z",
        "period_end": "2026-10-15T00:00:00Z",
    }
    cycle = {
        **record("a-new"),
        "episode_ids": ["episode-a"],
        "source_ids": ["s-a"],
        "input_digest": "a" * 64,
        "operation_id": "op-a",
        "stage": "done",
        "status": "completed",
        "record_ids": ["claim-a", "summary-a"],
        "error_code": None,
    }
    older = {
        **cycle,
        **record("z-old", "2026-10-14T09:00:00Z"),
        "stage": "extract",
        "status": "failed",
        "record_ids": [],
        "error_code": "CAPABILITY_UNAVAILABLE",
    }

    def route(request):
        parts = urlsplit(request.request.url)
        path, query = parts.path, parse_qs(parts.query)
        if request.request.method != "GET":
            posts.append(path)
            request.fulfill(status=400, json={"code": "INVALID_ARGUMENTS"})
            return
        if path == "/api/v1/auth/session":
            request.fulfill(
                json={
                    "principal_id": "operator",
                    "scopes": [WORK, TEST],
                    "csrf_token": "s" * 43,
                    "expires_at": FAR,
                }
            )
            return
        selected = query.get("persona_id", ["alpha"])[0]
        if path == "/api/v1/private-api-status":
            request.fulfill(
                json={
                    "scope": WORK,
                    "enabled": True,
                    "memory_processing": True,
                    "memory_daily_calls": 8,
                    "memory_calls_used": 2,
                    "dialogue_remaining": 290,
                    "period": "utc_day",
                    "attempt_limit": 300,
                    "attempts_used": 4,
                    "attempts_remaining": 296,
                    "resets_at": "2026-10-16T00:00:00Z",
                    "checked_at": "2026-10-15T09:00:00Z",
                }
            )
            return
        if path == "/api/v1/memory-cycles" and state["failed"]:
            request.fulfill(status=503, json={"code": "SERVICE_UNAVAILABLE"})
            return
        data = {
            "/api/v1/sessions": [run],
            "/api/v1/memory-cycles": [older, cycle],
            "/api/v1/claims": [claim],
            "/api/v1/summaries": [summary],
        }
        request.fulfill(
            json={
                "items": data.get(path, []) if selected == "alpha" else [],
                "limit": 50,
                "next_cursor": None,
                "complete": True,
            }
        )

    page.route("**/api/v1/**", route)
    page.goto(live_panel + "/#/dialogue")
    guide = page.get_by_role("region", name="Подсказки по панели")
    expect(guide).to_contain_text("нажмите START")
    expect(page.get_by_role("button", name="START — запустить агента", exact=True)).to_be_enabled()
    guide.get_by_role("button", name="Как пользоваться панелью", exact=True).click()
    expect(guide).to_contain_text("Если ответ не появился")
    guide.get_by_role("link", name="Память и люди", exact=True).click()
    panel = page.get_by_role("region", name="Обработка памяти и рефлексия")
    expect(panel).to_contain_text("Обработка ждёт запуска")
    expect(panel).to_contain_text("утверждений — 1, сводок — 1, предложений развития — 0")
    expect(panel.get_by_text("Обработка завершена", exact=False)).to_be_visible()
    expect(panel.get_by_text("Цикл не завершён", exact=True)).not_to_be_visible()
    panel.locator("summary").click()
    expect(panel).to_contain_text("CAPABILITY_UNAVAILABLE")
    expect(panel).to_contain_text("Кнопки «повторить» нет: продолжение решает ядро по сохранённому исходу")
    guide.get_by_role("button", name="Свернуть подсказки", exact=True).click()
    screenshot = Path(__file__).resolve().parents[2] / "runtime/panel-memory-status.png"
    screenshot.parent.mkdir(exist_ok=True)
    panel.screenshot(path=str(screenshot))
    state["failed"] = True
    panel.get_by_role("button", name="Обновить состояние памяти", exact=True).click()
    expect(panel).to_contain_text("Сохранённые ниже данные могут быть устаревшими")
    assert not posts and not errors
    state["failed"] = False
    panel.get_by_role("button", name="Обновить состояние памяти", exact=True).click()
    expect(
        panel.get_by_text("Сохранённые ниже данные могут быть устаревшими", exact=False)
    ).to_have_count(0)
    page.get_by_role("combobox", name="Просматриваемый персонаж и память", exact=True).click()
    page.get_by_role("option", name="beta · ТЕСТ", exact=False).click()
    expect(guide).to_contain_text("Открыта тестовая память")
    expect(panel).to_contain_text("Это тестовая область")
    expect(panel).not_to_contain_text("Вымышленная сводка")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.get_by_role("button", name="START — запустить агента", exact=True)).to_be_visible()
    page.evaluate("window.scrollTo(0, 0)")
    expect(guide.get_by_text("С чего начать", exact=True)).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    assert guide.bounding_box()["y"] >= page.locator(".workspace-header").bounding_box()["height"]
    screenshot = Path(__file__).resolve().parents[2] / "runtime/panel-guidance-memory.png"
    screenshot.parent.mkdir(exist_ok=True)
    page.screenshot(path=str(screenshot), full_page=True)
    assert not posts and not errors
