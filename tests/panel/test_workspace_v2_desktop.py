"""Desktop v2: one profile decision and real home files in native Edge; no model call."""

import asyncio
import base64
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from ai_agent_adapters.sandbox.docker import DockerTaskRunner, cli
from ai_agent_adapters.sandbox.home_files import HomeFiles
from host_support import Server, free_port
from playwright.sync_api import expect
from test_activity import WorkModel, prepared
from test_live import binding
from test_workplace_profile import PROFILE

from ai_agent_core.local_host.layout import paths
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.work_sessions import enable_work_sessions
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.work_session_home import volume
from ai_agent_core.opencode_ui.protocol import directory

pytestmark = pytest.mark.skipif(
    not (os.getenv("AW_HOME_IMAGE_ID") and os.getenv("AW_NATIVE_UI_DIR")),
    reason="explicit image and native bundle required",
)
ROOT = Path(__file__).resolve().parents[2]


async def setup(tmp_path, monkeypatch):
    import quality_support

    original_init = quality_support.initialise
    workspace = "desktop-acceptance-" + uuid4().hex

    async def isolated_init(root, **kwargs):
        return await original_init(root, **{**kwargs, "workspace_id": workspace})

    # A home is keyed by scope, not tmp_path: never reuse a normal host's partition.
    monkeypatch.setattr(quality_support, "initialise", isolated_init)
    host = await prepared(tmp_path, monkeypatch, WorkModel())
    await enable_recovery(
        host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/e.sqlite"
    )
    await enable_workplace_files(host.root, ceiling_days=365)
    await enable_work_sessions(
        host.root,
        image_id=os.environ["AW_HOME_IMAGE_ID"],
        areas=["inbox", "projects"],
        output_area="projects",
        ceiling_days=365,
        home_enabled=True,
        resident_enabled=True,
        egress_enabled=True,
        helper_enabled=True,
        wake_enabled=True,
    )
    target = volume(host.scope)
    await DockerTaskRunner.ensure_home(target)
    await cli(
        "run",
        "--rm",
        "--network=none",
        "--user=65532:65532",
        "--mount",
        f"type=volume,src={target},dst=/workspace",
        "--entrypoint=/usr/local/bin/python",
        os.environ["AW_HOME_IMAGE_ID"],
        "-I",
        "-c",
        "from pathlib import Path; Path('/workspace/inbox').mkdir(exist_ok=True)",
    )
    assert (
        await HomeFiles(os.environ["AW_HOME_IMAGE_ID"]).put(
            target, "desktop-note.md", b"Synthetic Desktop home marker"
        )
    )["status"] == "ok"
    return host, target


def test_profile_and_real_home_tree_in_edge(page, tmp_path, monkeypatch):
    with ThreadPoolExecutor(max_workers=1) as executor:
        host, target = executor.submit(lambda: asyncio.run(setup(tmp_path, monkeypatch))).result()
    try:
        with Server(
            host.root,
            free_port(),
            tmp_path / "serve.log",
            provider="live",
            panel_dir=ROOT / "apps/panel/dist",
            env_file=binding(tmp_path / "synthetic.env"),
            opencode_ui_dir=Path(os.environ["AW_NATIVE_UI_DIR"]),
        ) as server:
            origin = server.origin
            page.goto(origin + "/#/workspace")
            page.get_by_label("Пароль", exact=True).fill(
                paths(host.root).password.read_text().strip()
            )
            page.get_by_role("button", name="Войти", exact=True).click()
            expect(page.get_by_role("button", name="START — запустить агента")).to_be_visible(
                timeout=30000
            )
            for cap in PROFILE:
                expect(page.get_by_role("listitem", name=cap)).to_be_visible(timeout=20000)
            page.get_by_role("button", name="Подготовить начальный доступ").click()
            plan = page.get_by_label("План последствий")
            plan.get_by_label("Я понимаю").check()
            plan.get_by_role("button", name="Выдать доступ", exact=True).click()
            for cap in PROFILE:
                expect(
                    page.get_by_role("listitem", name=cap).get_by_text("Разрешено", exact=True)
                ).to_be_visible(timeout=20000)
            csrf = page.request.get(origin + "/api/v1/auth/session").json()["csrf_token"]
            bridge = origin + "/api/v1/opencode/agents/" + directory(host.scope)
            reply = page.request.post(
                bridge + "/commands",
                headers={"Origin": origin, "x-csrf-token": csrf},
                data=dict(
                    type="work.session.open",
                    request_id="desktop-open",
                    target="work",
                    scope=host.scope.model_dump(mode="json"),
                    arguments=dict(
                        schema_id="work.session.open-args", data={"title": "Desktop home files"}
                    ),
                ),
            )
            assert reply.status == 200
            slug = base64.urlsafe_b64encode(directory(host.scope).encode()).decode().rstrip("=")
            page.goto(origin + "/opencode/" + slug + "/session/" + reply.json()["resource_id"])
            tree = page.locator('[data-component="filetree"]').first
            page.get_by_role("button", name="Файлы агента", exact=True).click()
            expect(tree.get_by_text("workspace", exact=True)).to_be_visible(timeout=30000)
            tree.get_by_text("workspace", exact=True).click()
            home_tree = (
                tree.locator('[data-scope="filetree"]')
                .filter(has=page.get_by_text("workspace", exact=True))
                .first
            )
            home_tree.get_by_text("inbox", exact=True).click()
            home_tree.get_by_text("desktop-note.md", exact=True).click()
            expect(
                page.get_by_text("Synthetic Desktop home marker", exact=True).first
            ).to_be_visible(timeout=15000)
            page.on(
                "dialog",
                lambda dialog: (
                    dialog.accept("workspace") if dialog.type == "prompt" else dialog.dismiss()
                ),
            )
            with page.expect_response(
                lambda r: "/home-inbox/desktop-upload.txt" in r.url, timeout=20000
            ) as uploaded:
                page.locator('input[type="file"]').first.set_input_files(
                    dict(
                        name="desktop-upload.txt", mimeType="text/plain", buffer=b"Synthetic upload"
                    )
                )
            assert uploaded.value.status == 200
            data = page.request.get(bridge + "/home-file/inbox/desktop-upload.txt").json()
            assert data["text"] == "Synthetic upload" and not data["truncated"]
            assert page.request.get(bridge + "/sessions").status == 200
    finally:
        result = subprocess.run(["docker", "volume", "rm", target], capture_output=True)
        assert result.returncode == 0
