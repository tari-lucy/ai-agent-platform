"""Native patched upstream UI against production owners; synthetic model, opt-in build."""

import base64
import json
import os
import time
from pathlib import Path

import httpx
import pytest
import test_work_conversation_ack_browser as support
from playwright.sync_api import expect
from test_live import answer
from test_work_conversation_ack_browser import work_host as work_host

from ai_agent_core.local_host.layout import paths
from ai_agent_core.opencode_ui.protocol import PREFIX, directory

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_NATIVE_UI_DIR"), reason="explicit packaged native OpenCode build required"
)
MESSAGE = "Синтетический вопрос для проверки перезагрузки."
REPLY = "Синтетический ответ: рабочая сессия доступна после запуска и перезагрузки."


@pytest.fixture(autouse=True)
def native_distribution(monkeypatch):
    original = support.build

    class NativeModel(support.WorkModel):
        async def __call__(self, request):
            body = json.loads(request.content)
            if self.role(body) != "activity":
                return await super().__call__(request)
            self.calls.append(("activity", body))
            assert self.queues["activity"], "unscripted work-session model call"
            payload = self.queues["activity"].pop(0)
            return httpx.Response(
                200,
                content=answer(payload["reply"]),
                headers={"Content-Type": "text/event-stream"},
            )

    def build(*args, **kwargs):
        kwargs["opencode_ui_dir"] = Path(os.environ["AW_NATIVE_UI_DIR"])
        if os.getenv("AW_PANEL_DIST"):
            kwargs["panel_dir"] = Path(os.environ["AW_PANEL_DIST"])
        return original(*args, **kwargs)

    monkeypatch.setattr(support, "build", build)
    monkeypatch.setattr(support, "WorkModel", NativeModel)


def test_native_reload_stopped_active_reply_and_stop(page, work_host):
    origin, host, model = work_host
    page.goto(origin + "/")
    page.get_by_label("Пароль", exact=True).fill(paths(host.root).password.read_text().strip())
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.get_by_role("button", name="START — запустить агента")).to_be_visible()
    csrf = page.request.get(origin + "/api/v1/auth/session").json()["csrf_token"]
    selected = directory(host.scope)
    target = origin + PREFIX + "/agents/" + selected

    def command(kind, request_id, resource, arguments, version=None):
        payload = dict(
            type=kind,
            request_id=request_id,
            target=resource,
            scope=host.scope.model_dump(mode="json"),
            arguments=arguments,
        )
        if version is not None:
            payload["expected_version"] = version
        response = page.request.post(
            target + "/commands", data=payload, headers={"Origin": origin, "x-csrf-token": csrf}
        )
        assert response.status == 200, response.text()
        return response.json()

    opened = command(
        "work.session.open",
        "native-open",
        "work",
        dict(schema_id="work.session.open-args", data={"title": "Синтетическая сессия"}),
    )["resource_id"]
    command(
        "work.session.message",
        "native-message",
        opened,
        dict(schema_id="work.session.message-args", data={"session_id": opened, "text": MESSAGE}),
        1,
    )
    slug = base64.urlsafe_b64encode(selected.encode()).decode().rstrip("=")
    url = origin + "/opencode/" + slug + "/session/" + opened
    page.goto(url)
    expect(page.get_by_text(MESSAGE, exact=True).first).to_be_visible(timeout=20000)
    page.reload()
    expect(page.get_by_text(MESSAGE, exact=True).first).to_be_visible(timeout=20000)
    assert model.count("activity") == 0

    page.add_init_script(
        "Object.defineProperty(document, 'visibilityState', {configurable:true, value:'hidden'})"
    )
    model.queues["activity"].append({"reply": REPLY})
    control = page.context.new_page()
    control.goto(origin + "/")
    control.get_by_role("button", name="START — запустить агента").click()
    control.close()
    page.reload()
    try:
        expect(page.get_by_text(MESSAGE, exact=True).first).to_be_visible(timeout=20000)
        expect(page.get_by_text(REPLY, exact=True).first).to_be_visible(timeout=30000)
    except AssertionError:
        view = page.request.get(target + "/sessions/" + opened).json()
        print(
            "NATIVE_OWNER_STATE",
            {
                "calls": model.count("activity"),
                "progress": view.get("progress"),
                "pending": bool(view.get("pending_reply")),
                "speakers": [entry["speaker"] for entry in view["entries"]],
            },
        )
        print("NATIVE_GATE_STATES", page.evaluate("() => globalThis.__awGateStates ?? {}"))
        print("NATIVE_BODY", page.inner_text("body")[:1600])
        raise
    view = page.request.get(target + "/sessions/" + opened).json()
    assert view["pending_reply"] and [e["speaker"] for e in view["entries"]] == ["operator"]
    page.set_viewport_size({"width": 1440, "height": 160})
    page.evaluate(
        "Object.defineProperty(document, 'visibilityState', {configurable:true, value:'visible'})"
    )
    page.wait_for_timeout(1200)  # One automatic-ACK timer while the reply is clipped.
    assert page.request.get(target + "/sessions/" + opened).json()["pending_reply"]
    page.set_viewport_size({"width": 1440, "height": 960})
    expect(page.get_by_text(REPLY, exact=True).first).to_be_visible()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        view = page.request.get(target + "/sessions/" + opened).json()
        if view["pending_reply"] is None:
            break
        time.sleep(0.1)
    assert view["pending_reply"] is None
    assert [entry["speaker"] for entry in view["entries"]] == ["operator", "character"]
    page.reload()
    expect(page.get_by_text(REPLY, exact=True).first).to_be_visible(timeout=20000)
    assert model.count("activity") == 1
    assert page.evaluate("() => [localStorage.length, sessionStorage.length]") == [0, 0]
    control = page.context.new_page()
    control.goto(origin + "/")
    control.get_by_role("button", name="STOP — остановить активного агента").click()
    expect(control.get_by_role("button", name="START — запустить агента")).to_be_visible()
    control.close()
