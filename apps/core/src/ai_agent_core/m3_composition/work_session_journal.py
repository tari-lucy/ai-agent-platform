"""The tool journal of work-conversation turns (W1): recorded observations, never authority.

A turn's finished tool calls are clipped by the trusted harness and kept in its WorkAttempt.
They are shown to the operator and summarised for the next turn only while the sources and
files the observed turn was based on are still released; the next turn then depends on them,
so a later revocation reaches it as well.
"""

from typing import TYPE_CHECKING

from ai_agent_application.agent_workplace.ports import HarnessEvent
from ai_agent_contracts.agent_sessions import WorkSession
from ai_agent_contracts.agent_workplace import ToolStep, WorkAttempt

from .workplace_experience import ticket_release

if TYPE_CHECKING:
    from ai_agent_application.agent_activity.book import ActivityBook

    from .service import Runtime

MAX_NOTE = 3000
LINE_INPUT = 160
LINE_OUTPUT = 200


async def keep_journal(
    owner: ActivityBook, attempt: WorkAttempt, events: list[HarnessEvent]
) -> WorkAttempt:
    found = next((e for e in events if e.steps or e.steps_omitted), None)
    if found is None:
        return attempt
    latest = await owner.attempts_repo(attempt.scope).get(attempt.id)
    if latest is None or latest.state != "submitted":
        return attempt
    return await owner.record_steps(latest, found.steps, found.steps_omitted)


async def released(rt: Runtime, attempt: WorkAttempt) -> bool:
    return await ticket_release(rt).bases_open(
        attempt.scope, attempt.inputs, attempt.input_files, "api_transfer"
    )


def replies(attempts: list[WorkAttempt]) -> dict[int, WorkAttempt]:
    """The succeeded turn behind each reply, keyed by the message it answered."""
    found: dict[int, WorkAttempt] = {}
    for a in sorted(attempts, key=lambda a: a.attempt_no):
        if a.state == "succeeded" and a.session_sequence is not None:
            found[a.session_sequence] = a
    return found


def _line(step: ToolStep) -> str:
    def short(text: str, limit: int) -> str:
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    mark = "ошибка" if step.status == "error" else "ok"
    return (
        f"- {step.tool} [{mark}] {short(step.input, LINE_INPUT)} → "
        f"{short(step.output, LINE_OUTPUT)} ({step.output_chars} симв.)"
    )


def note(attempt: WorkAttempt) -> str:
    head = (
        "Журнал инструментов твоего прошлого хода в этом разговоре — что действительно "
        "выполнялось и что вернулось (наблюдения, не права). Опирайся на них, а не на "
        "прежние утверждения в тексте."
    )
    lines = [head]
    if attempt.tool_steps_omitted:
        lines.append(f"(более ранние шаги не показаны: {attempt.tool_steps_omitted})")
    size = sum(map(len, lines))
    shown = []
    for step in reversed(attempt.tool_steps):
        line = _line(step)
        if size + len(line) > MAX_NOTE:
            break
        shown.append(line)
        size += len(line)
    lines += reversed(shown)
    return "\n".join(lines)


async def observations(
    rt: Runtime,
    owner: ActivityBook,
    session: WorkSession,
    attempt: WorkAttempt,
) -> tuple[str | None, WorkAttempt]:
    """A note on the latest earlier turn with a journal; the current attempt then depends
    on that turn's inputs. Nothing when the observed turn's basis is not released."""
    earlier = [
        a
        for a in await owner.session_attempts(session.scope, session.id)
        if a.id != attempt.id and (a.tool_steps or a.tool_steps_omitted)
    ]
    if not earlier:
        return None, attempt
    last = max(earlier, key=lambda a: (a.session_sequence or 0, a.attempt_no))
    if not await released(rt, last):
        return None, attempt
    attempt = await owner.extend_inputs(
        attempt, sources=tuple(last.inputs), files=tuple(last.input_files)
    )
    return note(last), attempt
