"""GLM file intake: operator uploads through the access boundary (OC file-intake task A).

Production init/build/ASGI with the workplace files owner; no model calls for uploads,
the agent stays STOPPED. Verified on a real temporary host:

- a raw octet-stream upload lands in the catalogue through the file owner: versioned,
  provenance "operator upload", replay of the same content is idempotent, new content
  under the same path becomes a new version;
- bridge/panel library projections expose area and path for the native file tree, and a
  second persona's projection does not list the first persona's file;
- the person's next work turn names the uploaded material in its instruction (data,
  not rights) while nothing whole-file enters the model context;
- refusals: areas outside inbox/knowledge/projects, a foreign scope, a missing CSRF
  token, a wrong content type and an over-quota body.
"""

import asyncio

from session_support import install
from test_activity import WorkModel, fast, prepared, until

from ai_agent_core import bootstrap
from ai_agent_core.local_host import rights
from ai_agent_core.local_host.layout import WorkplaceFilesConfig, load_config, paths
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.opencode_ui.protocol import PREFIX, directory

UPLOAD = "/api/v1/workplace-files/upload"
TWO_MIB = 2 * 1024 * 1024


def base(scope):
    return PREFIX + "/agents/" + directory(scope)


def opened(scope, key):
    return dict(
        type="work.session.open",
        request_id=key,
        target="work",
        scope=scope.model_dump(mode="json"),
        arguments=dict(schema_id="work.session.open-args", data={"title": "GLM файлы"}),
    )


def message(scope, identifier, *, text, key, version=1):
    return dict(
        type="work.session.message",
        request_id=key,
        target=identifier,
        scope=scope.model_dump(mode="json"),
        expected_version=version,
        arguments=dict(
            schema_id="work.session.message-args", data={"session_id": identifier, "text": text}
        ),
    )


async def upload(host, scope, area, name, content, *, title=None, headers=None, query=None):
    params = {
        "workspace_id": scope.workspace_id,
        "persona_id": scope.persona_id,
        "memory_space_id": scope.memory_space_id,
        "memory_mode": scope.memory_mode,
        "area": area,
        "name": name,
    }
    if title is not None:
        params["title"] = title
    if query is not None:
        params = {**params, **query}
    return await host.env.client.post(
        UPLOAD,
        params=params,
        content=content,
        headers={
            "Content-Type": "application/octet-stream",
            **(host.env.headers if headers is None else headers),
        },
    )


async def library(host, scope):
    result = await host.env.client.get(base(scope) + "/files")
    assert result.status_code == 200, result.text
    return result.json()


def test_operator_upload_catalogue_projections_notification_and_refusals(tmp_path, monkeypatch):
    async def scenario():
        # The configured per-file ceiling now accepts 100 MiB (task A3).
        WorkplaceFilesConfig(blob_dir="x", max_file_bytes=104_857_600)
        surfaces = []
        original = bootstrap.install_access

        def installed(*args, **kwargs):
            surface = original(*args, **kwargs)
            surfaces.append(surface)
            return surface

        monkeypatch.setattr(bootstrap, "install_access", installed)
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await enable_workplace_files(host.root, ceiling_days=7, max_file_bytes=TWO_MIB)
        async with host.open():
            rt, scope, env = host.runtime, host.scope, host.env
            blob = bytes(range(256)) * 2048  # 512 KiB of binary data

            # 1. A binary upload lands through the file owner while the agent is STOPPED.
            first = await upload(
                host, scope, "inbox", "report/data.bin", blob, title="Отчёт с данными"
            )
            assert first.status_code == 200, first.text
            card = first.json()
            assert card["version"] == 1 and card["area"] == "inbox"
            assert card["path"] == "report/data.bin" and card["kind"] == "material"
            assert card["provenance"] == "read"
            assert card["change_reason"] == "operator upload"
            assert card["produced_by_attempt"] is None
            assert rt.supervisor.snapshot.state.value == "STOPPED" and model.count() == 0

            # Same content again: the durable receipt replays, no second version.
            replay = await upload(
                host, scope, "inbox", "report/data.bin", blob, title="Отчёт с данными"
            )
            assert replay.status_code == 200, replay.text
            assert replay.json() == card
            # New content under the same path: a new version of the same file.
            changed = await upload(
                host, scope, "inbox", "report/data.bin", blob + b"tail", title="Отчёт с данными"
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["version"] == 2 and changed.json()["id"] == card["id"]

            restored = await upload(
                host, scope, "inbox", "report/data.bin", blob, title="Отчёт с данными"
            )
            assert restored.status_code == 200, restored.text
            assert restored.json()["version"] == 3
            assert restored.json()["reference"]["checksum"] == card["reference"]["checksum"]
            assert restored.json()["access"]["api_transfer"] is True
            assert restored.json()["access"]["public_disclosure"] is False

            # 2. Projections for the native tree and the panel carry area and path.
            listed = await library(host, scope)
            entry = next(f for f in listed["files"] if f["path"] == "report/data.bin")
            assert entry["area"] == "inbox" and entry["title"] == "Отчёт с данными"
            assert entry["version"] == 3 and entry["change_reason"] == "operator upload"
            projects = await upload(
                host, scope, "projects", "launch/plan.txt", b"plan v1", title="План запуска"
            )
            assert projects.status_code == 200, projects.text
            assert any(f["area"] == "projects" for f in (await library(host, scope))["files"])

            # 3. A second persona's projection does not list the first persona's file.
            inactive = scope.model_copy(
                update={"persona_id": "glm-files-beta", "memory_space_id": "glm-files-beta"}
            )
            await rt.store.provision(inactive, rights.host_grants(load_config(paths(host.root))))
            rt.scopes = (*rt.scopes, inactive)
            surfaces[-1].sessions.scopes = (*surfaces[-1].sessions.scopes, inactive)
            beta_library = await library(host, inactive)
            assert not [f for f in beta_library["files"] if f["area"] == "inbox"]

            # 4. The next work turn names the material as data; no whole file in context.
            fast(rt)
            await install(rt, scope, areas=["inbox", "projects"])
            sid = (
                await env.client.post(
                    base(scope) + "/commands",
                    json=opened(scope, "glm-files-open"),
                    headers=host.env.headers,
                )
            ).json()["resource_id"]
            request = message(
                scope, sid, text="Посмотри материалы, пожалуйста", key="glm-files-say"
            )
            said = await env.client.post(
                base(scope) + "/commands", json=request, headers=host.env.headers
            )
            assert said.status_code == 200, said.text
            model.queues["activity"].append({"reply": "Синтетический ответ по материалам"})
            run = rt.supervisor.snapshot
            start = dict(
                type="agent.start",
                request_id="glm-files-start",
                target="primary",
                scope=run.scope.model_dump(mode="json"),
                expected_version=run.version,
                arguments={"autonomy": "autonomous"},
            )
            assert (
                await env.client.post(
                    base(scope) + "/commands", json=start, headers=host.env.headers
                )
            ).status_code == 200

            async def read_view():
                result = await env.client.get(base(scope) + "/sessions/" + sid)
                assert result.status_code == 200, result.text
                return result.json()

            await until(
                read_view,
                lambda v: v.get("pending_reply") and v["pending_reply"]["acknowledgeable"],
                "material turn",
                seconds=60,
            )
            prompt = model.last("activity")["messages"][-1]["content"]
            assert "Материалы, переданные оператором" in prompt
            assert "Отчёт с данными" in prompt and "План запуска" in prompt
            assert "report/data.bin" in prompt
            assert bytes(range(256)).hex()[:8] not in prompt  # content itself never enters

            # 5. Refusals.
            foreign = dict(persona_id="another-person", memory_space_id="another-memory")
            assert (
                await upload(host, scope, "exports", "out.bin", b"x")
            ).status_code == 403  # not an operator area
            assert (await upload(host, scope, "scratch", "tmp.bin", b"x")).status_code == 403
            assert (
                await upload(host, scope, "inbox", "foreign.bin", b"x", query=foreign)
            ).status_code == 403
            no_csrf = {k: v for k, v in host.env.headers.items() if k != "X-CSRF-Token"}
            assert (
                await upload(host, scope, "inbox", "no-csrf.bin", b"x", headers=no_csrf)
            ).status_code == 403
            wrong_type = await env.client.post(
                UPLOAD,
                params={
                    "workspace_id": scope.workspace_id,
                    "persona_id": scope.persona_id,
                    "memory_space_id": scope.memory_space_id,
                    "memory_mode": scope.memory_mode,
                    "area": "inbox",
                    "name": "json.bin",
                },
                content=b"x",
                headers={**host.env.headers, "Content-Type": "application/json"},
            )
            assert wrong_type.status_code == 400
            assert (
                await upload(host, scope, "inbox", "big.bin", b"x" * (TWO_MIB + 1))
            ).status_code == 429  # above the configured per-file quota
            bad_name = await upload(host, scope, "inbox", "Отчёт!.bin", b"x")
            assert bad_name.status_code == 400  # logical path: lowercase ascii only
            run = rt.supervisor.snapshot
            stopped = await env.client.post(
                "/api/v1/agents/" + run.agent_id + "/stop",
                headers=host.env.headers,
                json=dict(
                    type="agent.stop",
                    request_id="glm-files-stop",
                    target=run.agent_id,
                    scope=run.scope.model_dump(mode="json"),
                ),
            )
            assert stopped.status_code == 200
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            assert model.count("activity") == 1  # uploads never called the model

            # A slow upload has its own lane: STOP responds before the body finishes.
            waiting, finish = asyncio.Event(), asyncio.Event()

            async def body():
                yield b"first"
                waiting.set()
                await finish.wait()
                yield b"last"

            pending_upload = asyncio.create_task(
                upload(host, scope, "inbox", "during-stop.txt", body())
            )
            await asyncio.wait_for(waiting.wait(), 3)
            current = rt.supervisor.snapshot
            try:
                stop_during = await asyncio.wait_for(
                    env.client.post(
                        "/api/v1/agents/" + current.agent_id + "/stop",
                        headers=host.env.headers,
                        json=dict(
                            type="agent.stop",
                            request_id="glm-stop-during-upload",
                            target=current.agent_id,
                            scope=current.scope.model_dump(mode="json"),
                        ),
                    ),
                    3,
                )
                assert stop_during.status_code == 200, stop_during.text
            finally:
                finish.set()
            assert (await pending_upload).status_code == 200

            # A session revoked during the body must not publish a file afterwards.
            from ai_agent_core.access import surface as access_module

            original_octet = access_module._octet

            async def logout_after_body(headers, receive, maximum):
                result = await original_octet(headers, receive, maximum)
                current = surfaces[-1].operator(headers, mutation=True)
                surfaces[-1].sessions.logout(current)
                return result

            monkeypatch.setattr(access_module, "_octet", logout_after_body)
            revoked = await upload(host, scope, "inbox", "after-logout.txt", b"not published")
            assert revoked.status_code == 403, revoked.text

    asyncio.run(scenario())
