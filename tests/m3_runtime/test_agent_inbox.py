"""The operator hands a file to the persona: safe name, no overwrite, her folder only."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from access_support import ORIGIN, PASSWORD, configured_hash
from ai_agent_adapters.configuration import BootstrapSettings
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from composition_support import run, setup

from ai_agent_core.m3_composition.agent_inbox import AgentInbox, safe_name, store
from ai_agent_core.m3_composition.app import create_m3_app


def test_names_lose_directories_and_dangerous_characters_but_keep_the_extension():
    assert safe_name("отчёт.pdf") == "отчёт.pdf"
    assert safe_name("C:\\Users\\me\\Downloads\\scan 1.png") == "scan 1.png"
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name(".hidden") == "hidden"
    assert safe_name('a<b>:c|"d?.txt') == "a_b__c__d_.txt"
    long = safe_name("x" * 400 + ".zip")
    assert len(long) <= 180 and long.endswith(".zip")
    for bad in ("", "   ", "..", "...", "/", "\\"):
        with pytest.raises(ContractError):
            safe_name(bad)


def test_a_file_is_never_overwritten_and_leaves_no_part_file(tmp_path):
    assert store(tmp_path, "scan.png", b"one") == "scan.png"
    assert store(tmp_path, "scan.png", b"two") == "scan (2).png"
    assert store(tmp_path, "scan.png", b"three") == "scan (3).png"
    assert store(tmp_path, "notes", b"x") == "notes"
    assert store(tmp_path, "notes", b"y") == "notes (2)"
    inbox = tmp_path / "inbox"
    assert (inbox / "scan.png").read_bytes() == b"one"
    assert (inbox / "scan (2).png").read_bytes() == b"two"
    assert not [p for p in inbox.iterdir() if p.name.startswith(".part-")]
    assert oct((inbox / "scan.png").stat().st_mode & 0o777) == "0o664"


def runtime(folder, persona="p"):
    scope = SimpleNamespace(persona_id=persona)
    host = SimpleNamespace(folders=lambda: folder)
    return SimpleNamespace(
        harness=host, supervisor=SimpleNamespace(snapshot=SimpleNamespace(scope=scope))
    )


def test_the_owner_writes_only_for_the_active_persona_and_only_when_a_folder_exists(tmp_path):
    async def scenario():
        mine = SimpleNamespace(persona_id="p")
        entry = await AgentInbox(runtime(tmp_path)).put(mine, "a.txt", b"hi")  # type: ignore[arg-type]
        assert entry == {"name": "a.txt", "path": "/workspace/inbox/a.txt", "size": 2}
        other = SimpleNamespace(persona_id="someone-else")
        for rt, scope, code in (
            (runtime(tmp_path), other, ErrorCode.ACCESS_DENIED),
            (runtime(None), mine, ErrorCode.CAPABILITY_UNAVAILABLE),
            (SimpleNamespace(harness=None), mine, ErrorCode.CAPABILITY_UNAVAILABLE),
        ):
            with pytest.raises(ContractError) as error:
                await AgentInbox(rt).put(scope, "a.txt", b"hi")  # type: ignore[arg-type]
            assert error.value.error.code == code
        with pytest.raises(ContractError):
            await AgentInbox(runtime(tmp_path)).put(mine, "empty.txt", b"")  # type: ignore[arg-type]

    asyncio.run(scenario())


def test_the_route_needs_a_session_a_csrf_token_and_the_work_scope(tmp_path):
    async def scenario():
        env = await setup(tmp_path, start=False)
        await env.runtime.close()
        scope = env.world.scope
        settings = BootstrapSettings(
            workspace_id=scope.workspace_id,
            persona_id=scope.persona_id,
            operator_origin=ORIGIN,
            operator_password_hash=configured_hash(),
        )
        app = create_m3_app(
            settings,
            env.world.store,
            env.world.registry,
            providers=lambda _: env.world.provider,
            scopes=(scope,),
        )
        async with app.router.lifespan_context(app):
            rt = app.state.runtime
            folder = tmp_path / "files"

            async def close() -> None:
                return None

            rt.harness = SimpleNamespace(folders=lambda: folder, tick=lambda: None, close=close)
            query = "&".join(
                f"{key}={getattr(scope, key)}"
                for key in ("workspace_id", "persona_id", "memory_space_id", "memory_mode")
            )
            url = f"/api/v1/agent-inbox?{query}&name=%D0%BE%D1%82%D1%87%D1%91%D1%82.txt"
            octet = {"Content-Type": "application/octet-stream", "Origin": ORIGIN}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as client:
                assert (await client.post(url, content=b"x", headers=octet)).status_code in (
                    401,
                    403,
                )  # no session
                login = await client.post(
                    "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
                )
                assert login.status_code == 200
                assert (await client.post(url, content=b"x", headers=octet)).status_code == 403
                headers = {**octet, "X-CSRF-Token": login.json()["csrf_token"]}
                if scope.memory_mode == "work":
                    done = await client.post(url, content=b"hello", headers=headers)
                    assert done.status_code == 200, done.text
                    assert done.json()["name"] == "отчёт.txt"
                    assert (folder / "inbox" / "отчёт.txt").read_bytes() == b"hello"
                else:  # the synthetic scope is TEST: refused, nothing written
                    denied = await client.post(url, content=b"hello", headers=headers)
                    assert denied.status_code == 403
                    assert not (folder / "inbox").exists()

    run(scenario())
