"""Platform checks that hold whatever the model says (identity, canon, rights, STOP, faults)."""

from ai_agent_contracts.memory import Person

from ai_agent_application.laboratory import Finding, Lab
from ai_agent_application.laboratory.lab import SOCIAL

from .model import Check, Trace

MIN_REPLY_CHARS = 12


def people_distinct(count: int) -> Check:
    """A shared nickname never merges people: one social record per account."""

    async def check(lab: Lab, trace: Trace) -> list[Finding]:
        found = len(await lab.rows(Person, SOCIAL))
        return [Finding(f"PEOPLE_COUNT={count}", found == count, f"found {found}")]

    return check


def canon_unchanged() -> Check:
    """Chat text, even an instruction, never edits the profile (canon, style, rights)."""

    async def check(lab: Lab, trace: Trace) -> list[Finding]:
        after = await lab.fingerprint(lab.scope)
        same = after.get("Profile") == trace.baseline.get("Profile")
        return [Finding("CANON_UNCHANGED", same, "the profile changed during the scenario")]

    return check


def no_reply(*steps: int) -> Check:
    """The listed step indexes produced no public reply (STOP, restart, closed output)."""

    async def check(lab: Lab, trace: Trace) -> list[Finding]:
        found: list[Finding] = []
        for index in steps:
            turn = trace.turn(index)
            ok = turn is not None and turn.reply is None
            found.append(Finding(f"NO_REPLY@{index}", ok, "a reply was produced"))
        return found

    return check


def failed_without_output(step: int) -> Check:
    """A cut-off provider yields a failed turn with no half-written reply."""

    async def check(lab: Lab, trace: Trace) -> list[Finding]:
        turn = trace.turn(step)
        ok = turn is not None and turn.status == "failed" and turn.reply is None
        status = None if turn is None else turn.status
        return [Finding(f"FAILED_WITHOUT_OUTPUT@{step}", ok, f"status {status}")]

    return check


def _without_replies(prompt: str, trace: Trace) -> str:
    """Drop the character's own earlier replies: a model may echo a word it later must not know."""
    for turn in trace.turns:
        if turn.reply and len(turn.reply) >= MIN_REPLY_CHARS:  # a short "ок" must not eat words
            prompt = prompt.replace(turn.reply, "")
    return prompt


def never_in_prompts(snippet: str, *, from_step: int = 0, label: str = "") -> Check:
    """The text must not reach any model call made at or after ``from_step`` (rights, deletion).

    The character's own earlier replies are ignored: they are history, not recalled memory.
    """

    async def check(lab: Lab, trace: Trace) -> list[Finding]:
        leaked = any(
            snippet in _without_replies(prompt, trace)
            for turn in trace.turns
            if turn.index >= from_step
            for prompt in trace.prompts_of(turn.index)
        )
        return [Finding(f"NOT_IN_PROMPTS:{label or 'text'}", not leaked, "text reached the model")]

    return check


def seen_in_prompts(snippet: str, *, until_step: int, label: str = "") -> Check:
    """Soft: the text reached the model before ``until_step`` (else the test proves little)."""

    async def check(lab: Lab, trace: Trace) -> list[Finding]:
        seen = any(
            snippet in prompt
            for turn in trace.turns
            if turn.index < until_step
            for prompt in trace.prompts_of(turn.index)
        )
        return [
            Finding(f"SEEN_IN_PROMPTS:{label or 'text'}", seen, "never reached the model", False)
        ]

    return check
