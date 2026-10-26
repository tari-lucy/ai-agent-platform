"""W1 turn journal: production host owners, synthetic harness/provider only.

- the finished tool calls of a turn are kept by the activity owner and shown with the reply;
- the next turn gets them as observations and then depends on the observed turn's basis;
- once that basis is revoked the journal is hidden and no longer offered to the model.
"""

import asyncio

from ai_agent_application.agent_workplace.ports import HarnessEvent, ModelRequest
from ai_agent_application.llm import ChatMessage
from ai_agent_contracts.agent_workplace import ToolStep
from session_support import ack, install
from test_activity import ACTIVITY_MARKER, WorkModel, fast, prepared, until
from test_context_revocation import attempts, ready, revoke, rows
from test_work_sessions_http import open_session, say, transcript

NOTE = "Журнал инструментов твоего прошлого хода"


def journaling(rt, seen):
    class Harness:
        def __init__(self, gateway):
            self.gateway = gateway

        async def turn(self, gate, turn):
            seen.append(turn.instruction)
            reply = await self.gateway.generate(
                gate,
                ModelRequest(
                    "activity",
                    (ChatMessage("system", ACTIVITY_MARKER), ChatMessage("user", turn.instruction)),
                    max_output_tokens=64,
                    call_id="session-answer",
                ),
            )
            step = ToolStep(
                tool="bash",
                status="completed",
                input='{"command": "python3 parse.py"}',
                output=f"Synthetic marker turn {len(seen)}",
                output_chars=23,
            )
            if reply.status != "completed":
                return []
            return [HarnessEvent("result", reply.text, steps=(step,), steps_omitted=1)]

        async def close(self, key):
            pass

        async def cancel(self, key):
            pass

        async def staged(self, gate, output):
            raise AssertionError("Synthetic harness has no staged files")

    async def factory(execution, gateway, gate):
        return Harness(gateway)

    rt.session_harness = factory


def test_turn_journal_is_shown_observed_next_and_closed_with_its_basis(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        async with host.open():
            rt, scope = host.runtime, host.scope
            fast(rt)
            await install(rt, scope)
            seen: list[str] = []
            journaling(rt, seen)
            sid = (await open_session(host, "journal")).json()["resource_id"]
            assert (await say(host, "m1", sid, 1, "Synthetic first question")).status_code == 200
            model.queues["activity"].append({"reply": "Synthetic first answer"})
            await host.start()
            view = await until(lambda: transcript(host, sid), ready, "first draft", seconds=60)
            assert NOTE not in seen[0]
            assert (await ack(host, view["pending_reply"], "a1")).status_code == 200
            view = await transcript(host, sid)
            reply = view["entries"][-1]
            assert reply["speaker"] == "character" and reply["steps_omitted"] == 1
            assert [s["output"] for s in reply["steps"]] == ["Synthetic marker turn 1"]
            (first,) = await attempts(rt, scope, sid)
            assert first.tool_steps[0].tool == "bash" and first.tool_steps_omitted == 1

            # The next turn sees what was actually executed and depends on that turn's basis.
            model.queues["activity"].append({"reply": "Synthetic second answer"})
            said = await say(host, "m2", sid, view["session_version"], "Synthetic second question")
            assert said.status_code == 200
            view = await until(lambda: transcript(host, sid), ready, "second draft", seconds=60)
            assert NOTE in seen[1] and "Synthetic marker turn 1" in seen[1]
            assert "python3 parse.py" in seen[1]
            second = next(a for a in await attempts(rt, scope, sid) if a.id != first.id)
            assert {(r.source_id, r.version) for r in first.inputs} <= {
                (r.source_id, r.version) for r in second.inputs
            }
            assert (await ack(host, view["pending_reply"], "a2")).status_code == 200

            # The observed turn's basis is revoked: its journal is hidden and not observed again.
            await revoke(rt, (await rows(rt, sid))[0].source, "revoke-first")
            view = await transcript(host, sid)
            replies = [e for e in view["entries"] if e["speaker"] == "character"]
            assert any(not e["withheld"] for e in replies)
            assert all(e["steps"] == [] and e["steps_omitted"] == 0 for e in replies)
            model.queues["activity"].append({"reply": "Synthetic third answer"})
            said = await say(host, "m3", sid, view["session_version"], "Synthetic third question")
            assert said.status_code == 200
            await until(lambda: transcript(host, sid), ready, "third draft", seconds=60)
            assert NOTE not in seen[2] and "Synthetic marker" not in seen[2]

    asyncio.run(scenario())
