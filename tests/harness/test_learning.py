"""Real memory owner/SQLite; invented OpenCode messages, no Docker or paid calls."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.memory import Episode, Source
from ai_agent_contracts.states import ErrorCode, Provenance
from memory_support import MEMORY, NOW, command, make_scope, new_world

from ai_agent_core.m3_composition.harness_learning import HarnessLearning

SCOPE = make_scope()
TEXT = "Вымышленный маяк светит синим."


def row(mid, role, *, completed=True, text=TEXT, parent=None):
    stamp = int(NOW.timestamp() * 1000)
    return {
        "info": {
            "id": mid,
            "sessionID": "ses_a",
            "role": role,
            "parentID": parent,
            "time": {
                "created": stamp + (1 if role == "assistant" else 0),
                "completed": stamp + 2 if completed else None,
            },
        },
        "parts": [
            {"type": "text", "text": text},
            {"type": "tool", "text": "DO_NOT_IMPORT_TOOL_OR_FILE"},
            {"type": "reasoning", "text": "DO_NOT_IMPORT_REASONING"},
            {"type": "text", "synthetic": True, "text": "DO_NOT_IMPORT_CONTEXT"},
        ],
    }


class Host:
    def __init__(self):
        self.rows = [row("msg_b", "assistant", parent="msg_a"), row("msg_a", "user")]
        self.calls = []
        self.after_read = lambda: None

    async def opencode(self, method, path, body=None, *, timeout=60):
        self.calls.append((method, path))
        assert method == "GET" and body is None
        self.after_read()
        return [{"id": "ses_a"}] if path == "/session" else self.rows


class Ledger:
    def __init__(self):
        self.ids = set()

    async def entries(self, scope):
        return [SimpleNamespace(source_id=key) for key in self.ids]


def test_import_replay_and_crash_recovery_keep_one_source_and_observed_conversation(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [SCOPE], with_index=False)
        try:
            host, ledger, path = Host(), Ledger(), tmp_path / "cursor.json"

            def service():
                return HarnessLearning(
                    host, world.memory, ledger, path, lambda s: None, api_transfer=True
                )

            assert await service().tick(SCOPE) == 2
            assert await service().tick(SCOPE) == 0
            sources = await world.store.repository(Source, MEMORY, SCOPE).list(limit=20)
            episodes = await world.store.repository(Episode, MEMORY, SCOPE).list(limit=20)
            assert len(sources) == 2 and len(episodes) == 1
            assert {s.provenance for s in sources} == {
                Provenance.THIRD_PARTY_MESSAGE,
                Provenance.MODEL_PROPOSAL,
            }
            assert all(s.content == TEXT and not s.public_disclosure_allowed for s in sources)
            assert "не подтверждено" in episodes[0].actual_outcome
            assert TEXT not in path.read_text(encoding="utf-8")
            path.unlink()  # crash between owner commit and cursor write / restored cursor absent
            assert await service().tick(SCOPE) == 2
            assert len(await world.store.repository(Episode, MEMORY, SCOPE).list(limit=20)) == 1
            assert len(await world.store.repository(Source, MEMORY, SCOPE).list(limit=20)) == 2
        finally:
            await world.store.close()

    asyncio.run(scenario())


def test_unfinished_answer_waits_and_delete_never_reimports_even_without_cursor(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [SCOPE], with_index=False)
        try:
            host, ledger, path = Host(), Ledger(), tmp_path / "cursor.json"
            host.rows[0] = row("msg_b", "assistant", completed=False, parent="msg_a")
            learning = HarnessLearning(host, world.memory, ledger, path, lambda s: None)
            assert await learning.tick(SCOPE) == 1
            host.rows[0]["info"]["time"]["completed"] = int(NOW.timestamp() * 1000) + 2
            assert await learning.tick(SCOPE) == 1
            source = (await world.store.repository(Source, MEMORY, SCOPE).list(limit=20))[0]
            ledger.ids.add(source.id)
            await world.memory.delete_source(command(SCOPE), source.id, source.version)
            assert await learning.tick(SCOPE) == 0
            assert (await learning.notices(SCOPE))["ses_a"].reason == "source_unavailable"
            path.unlink()
            await learning.tick(SCOPE)
            assert await world.memory.get_source(SCOPE, source.id) is None
            assert (await learning.notices(SCOPE))["ses_a"].reason == "source_unavailable"
        finally:
            await world.store.close()

    asyncio.run(scenario())


def test_stop_during_api_read_and_other_partition_cannot_commit(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [SCOPE], with_index=False)
        try:
            active = True

            def guard(scope):
                if not active:
                    raise ContractError(ErrorCode.STALE_CONTEXT)

            def stop():
                nonlocal active
                active = False

            host, path = Host(), tmp_path / "cursor.json"
            host.after_read = stop
            learning = HarnessLearning(host, world.memory, Ledger(), path, guard)
            with pytest.raises(ContractError):
                await learning.tick(SCOPE)
            assert await world.store.repository(Source, MEMORY, SCOPE).list(limit=20) == []
            active = True
            with pytest.raises(ContractError):
                await learning.tick(make_scope("another-persona"))
            assert host.calls == [("GET", "/session")]
            await learning.close()
            with pytest.raises(ContractError):
                await learning.tick(SCOPE)
        finally:
            await world.store.close()

    asyncio.run(scenario())


def test_changed_message_and_corrupt_checkpoint_are_visible_failures(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [SCOPE], with_index=False)
        try:
            host, path = Host(), tmp_path / "cursor.json"
            learning = HarnessLearning(host, world.memory, Ledger(), path, lambda s: None)
            await learning.tick(SCOPE)
            host.rows[1]["parts"][0]["text"] = "Изменённая реплика"
            assert await learning.tick(SCOPE) == 0
            assert (await learning.notices(SCOPE))["ses_a"].reason == "message_changed"
            path.write_text(json.dumps({"version": 999}), encoding="utf-8")
            with pytest.raises(ValueError):
                await learning.tick(SCOPE)
        finally:
            await world.store.close()

    asyncio.run(scenario())


def test_old_history_does_not_starve_an_independent_fresh_session(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [SCOPE], with_index=False)
        try:
            host = Host()
            old = row("msg_old", "user")
            old["info"]["sessionID"] = "ses_old"
            old["info"]["time"]["created"] = int((NOW - timedelta(days=31)).timestamp() * 1000)

            async def read(method, path, body=None, *, timeout=60):
                if path == "/session":
                    return [{"id": "ses_old"}, {"id": "ses_a"}]
                return [old] if "ses_old" in path else host.rows

            host.opencode = read
            learning = HarnessLearning(
                host, world.memory, Ledger(), tmp_path / "cursor.json", lambda s: None
            )
            assert await learning.tick(SCOPE) == 2
            sources = await world.store.repository(Source, MEMORY, SCOPE).list(limit=20)
            assert len(sources) == 2
            assert (await learning.notices(SCOPE))["ses_old"].reason == "source_unavailable"
        finally:
            await world.store.close()

    asyncio.run(scenario())
