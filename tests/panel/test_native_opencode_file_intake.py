"""Edge vertical file intake: two personas, real HTTP/owners, packaged native UI."""

import asyncio
import base64
import os
from urllib.parse import parse_qs, urlsplit

import pytest
import test_work_conversation_ack_browser as support
from ai_agent_application.laboratory.profile import REFERENCE_PROFILE
from playwright.sync_api import expect
from test_native_opencode_sessions import native_distribution as native_distribution
from test_work_conversation_ack_browser import work_host as work_host

from ai_agent_core.local_host.layout import paths
from ai_agent_core.local_host.personas import add_persona
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.opencode_ui.protocol import PREFIX

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_NATIVE_UI_DIR") or os.getenv("AW_OPENCODE_IMAGE_ID"),
    reason="packaged UI with explicit synthetic harness required",
)
MARKER = "SYNTHETIC_BODY_ONLY_ON_EXPLICIT_READ_7241"


@pytest.fixture(autouse=True)
def file_world(monkeypatch, native_distribution, tmp_path):
    prepare, install, build = support.prepared, support.install, support.build
    shared = {}

    def observed_build(*args, **kwargs):
        app, closer = build(*args, **kwargs)
        shared.update(runtime=app.state.runtime, loop=asyncio.get_running_loop())
        return app, closer

    async def configured(directory, patch, model):
        host = await prepare(directory, patch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await enable_workplace_files(host.root, max_file_bytes=104857600)
        await add_persona(host.root, persona_id="beacon", profile_markdown=REFERENCE_PROFILE)
        return host

    async def fixture_install(rt, scope):
        await install(rt, scope, areas=["inbox", "knowledge", "projects"])

    monkeypatch.setattr(support, "prepared", configured)
    monkeypatch.setattr(support, "install", fixture_install)
    monkeypatch.setattr(support, "build", observed_build)
    return shared


def test_native_upload_tree_text_and_large_binary_keep_personas_isolated(
    page, work_host, tmp_path, file_world
):
    origin, host, model = work_host
    page.goto(origin + "/")
    page.get_by_label("Пароль", exact=True).fill(paths(host.root).password.read_text().strip())
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.get_by_role("button", name="START — запустить агента")).to_be_visible(timeout=20000)
    csrf = page.request.get(origin + "/api/v1/auth/session").json()["csrf_token"]
    catalogue = page.request.get(origin + PREFIX + "/agents").json()["items"]
    cards = {
        row["scope"]["persona_id"]: row
        for row in catalogue
        if row["scope"]["memory_mode"] == "work"
    }
    a, b = cards[host.scope.persona_id], cards["beacon"]

    def opened(card, key):
        result = page.request.post(
            origin + PREFIX + "/agents/" + card["directory"] + "/commands",
            headers={"Origin": origin, "x-csrf-token": csrf},
            data=dict(
                type="work.session.open",
                request_id=key.replace(" ", "-"),
                target="work",
                scope=card["scope"],
                arguments=dict(schema_id="work.session.open-args", data=dict(title=key)),
            ),
        )
        assert result.status == 200, result.text()
        slug = base64.urlsafe_b64encode(card["directory"].encode()).decode().rstrip("=")
        return origin + "/opencode/" + slug + "/session/" + result.json()["resource_id"]

    def library(card):
        response = page.request.get(origin + PREFIX + "/agents/" + card["directory"] + "/files")
        assert response.status == 200, response.text()
        return response.json()["files"]

    def attach(file, area):
        def choose(dialog):
            dialog.accept(area)

        page.once("dialog", choose)
        with page.expect_response(
            lambda response: "/api/v1/workplace-files/upload?" in response.url, timeout=120000
        ) as response:
            page.locator('input[type="file"]').first.set_input_files(file)
        assert response.value.status == 200, response.value.text()
        # Chromium may evict inspector bodies after a 90 MiB upload. The authoritative
        # catalogue confirms the exact file without depending on DevTools retention.
        query = parse_qs(urlsplit(response.value.url).query)
        target = cards[query["persona_id"][0]]
        return next(
            row
            for row in library(target)
            if row["area"] == area and row["path"] == query["name"][0]
        )

    page.goto(opened(b, "Files B"))
    expect(page.locator('[data-component="prompt-input"][contenteditable="true"]')).to_be_visible(
        timeout=30000
    )
    beta = attach(
        dict(name="заметки.txt", mimeType="text/plain", buffer=b"SYNTHETIC_BETA_MATERIAL"),
        "knowledge",
    )
    assert beta["scope"]["persona_id"] == "beacon"
    assert beta["id"] not in {row["id"] for row in library(a)}
    assert beta["id"] in {row["id"] for row in library(b)}

    page.goto(opened(a, "Files A"))
    expect(page.locator('[data-component="prompt-input"][contenteditable="true"]')).to_be_visible(
        timeout=30000
    )
    text = attach(
        dict(name="safeplan.txt", mimeType="text/plain", buffer=MARKER.encode()), "knowledge"
    )
    assert text["scope"]["persona_id"] == host.scope.persona_id
    large = tmp_path / "large-fixture.bin"
    with large.open("wb") as handle:
        handle.seek(90 * 1024 * 1024 - 1)
        handle.write(b"\0")
    binary = attach(str(large), "inbox")
    assert binary["reference"]["size_bytes"] == 90 * 1024 * 1024
    assert binary["id"] not in {row["id"] for row in library(b)}
    assert model.count("activity") == 0

    tree_button = page.locator('button[aria-controls="file-tree-panel"]').first
    if tree_button.get_attribute("aria-expanded") != "true":
        tree_button.click()
    tree = page.locator('[data-component="filetree"]').first
    expect(tree.get_by_text("knowledge", exact=True)).to_be_visible(timeout=15000)
    tree.get_by_text("knowledge", exact=True).click()
    expect(
        page.locator('[data-component="filetree"]').get_by_text("safeplan.txt", exact=True)
    ).to_be_visible(timeout=15000)
    page.locator('[data-component="filetree"]').get_by_text("safeplan.txt", exact=True).click()
    expect(page.get_by_text(MARKER, exact=True).first).to_be_visible(timeout=15000)
    # A second upload must refresh an already expanded tree without page reload.
    extra = attach(
        dict(name="followup.txt", mimeType="text/plain", buffer=b"SYNTHETIC_FOLLOWUP"), "knowledge"
    )
    expect(
        page.locator('[data-component="filetree"]').get_by_text("followup.txt", exact=True)
    ).to_be_visible(timeout=15000)
    assert extra["id"] in {row["id"] for row in library(a)}
    assert model.count("activity") == 0
    assert page.evaluate("() => [localStorage.length, sessionStorage.length]") == [0, 0]

    # Sending only the reference, then the shared context names the file; never bulk body.
    prompt = page.locator('[data-component="prompt-input"][contenteditable="true"]')
    prompt.fill("Материалы переданы. Подтверди, что видишь их названия.")
    model.queues["activity"].append({"reply": "Синтетический ответ: материалы доступны."})
    page.locator('[data-action="prompt-submit"]').click()
    control = page.context.new_page()
    control.goto(origin + "/")
    control.get_by_role("button", name="START — запустить агента").click()
    expect(control.locator(".active-run")).to_contain_text("Работает", timeout=15000)
    expect(page.get_by_text("Синтетический ответ: материалы доступны.", exact=True)).to_be_visible(
        timeout=30000
    )
    body = model.last("activity")
    assert "safeplan.txt" in str(body) and text["id"] in str(body)
    assert MARKER not in str(body) and "SYNTHETIC_BETA_MATERIAL" not in str(body)
    control.get_by_role("button", name="STOP — остановить активного агента").click()
    expect(control.get_by_role("button", name="START — запустить агента")).to_be_visible(
        timeout=15000
    )
    control.close()

    # The source owner withdraws the open file: cached text and tree must disappear.
    async def block_file():
        from ai_agent_application.storage import Owner
        from ai_agent_contracts.agent_workplace import ManagedFile

        from ai_agent_core.m3_composition.state import PRINCIPALS, put

        rt = file_world["runtime"]
        row = await rt.store.repository(ManagedFile, PRINCIPALS[Owner.CATALOGUE], host.scope).get(
            text["id"]
        )
        changed = row.model_copy(
            update={
                "version": row.version + 1,
                "state": "blocked",
                "reference": row.reference.model_copy(update={"version": row.version + 1}),
                "updated_at": rt.supervisor.clock.utc(),
            }
        )
        await put(
            rt.store,
            rt.registry,
            Owner.CATALOGUE,
            host.scope,
            "synthetic-block-file",
            changed,
            row.version,
            rt.readers,
        )
        rt.notify("work.changed", host.scope, changed.id, changed.version)

    asyncio.run_coroutine_threadsafe(block_file(), file_world["loop"]).result(timeout=15)
    expect(page.get_by_text(MARKER, exact=True)).to_have_count(0, timeout=15000)
