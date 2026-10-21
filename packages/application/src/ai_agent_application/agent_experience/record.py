"""Results of workplace steps returned to the memory owner (AW-06).

Only a succeeded tool step that produced files through their owner (receipts: the written
file versions) becomes an observation source and the basis of one confirmed episode. The
agent's own conclusion on finishing a task is kept as an interpretation source, never as an
episode or independent experience; plans, attempts and failures are not recorded here.
Everything is keyed by the attempt, so a retry or replay does not add a second basis. The
executor never calls this: the activity owner does, after the step. TEST runs record
``simulation`` in their own partition only.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ai_agent_contracts.agent_workplace import WorkStep, WorkTask
from ai_agent_contracts.api import CommandBase, MessageArguments, MessageCommand
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.states import Provenance

from ai_agent_application.dialogue.ports import MemoryPort
from ai_agent_application.memory import EpisodeDraft, SourceDraft

ADAPTER = "workplace-activity"


@dataclass(frozen=True)
class Recorded:
    source: SourceRef
    episode_id: str | None


def _command(scope: Scope, request_id: str) -> CommandBase:
    return MessageCommand(
        request_id=request_id[:128],
        scope=scope,
        target="workplace",
        type="dialogue.message",
        arguments=MessageArguments(text=request_id[:128]),
    )


class ExperienceRecorder:
    def __init__(self, memory: MemoryPort, clock: Callable[[], datetime]) -> None:
        self.memory, self.clock = memory, clock

    async def _transferable(self, scope: Scope, refs: tuple[SourceRef, ...]) -> bool:
        for ref in refs:
            found = await self.memory.get_source(scope, ref.source_id)
            if found is None or not found.api_transfer_allowed:
                return False
        return True

    async def _source(
        self, scope: Scope, key: str, kind: str, provenance: Provenance, text: str, api: bool
    ) -> SourceRef:
        made = await self.memory.register_source(
            _command(scope, "experience-" + key),
            SourceDraft(
                provenance=provenance,
                source_type=kind,
                adapter_id=ADAPTER,
                adapter_version="1",
                occurred_at=self.clock(),
                content=text,
                read_allowed=True,
                api_transfer_allowed=api,
                public_disclosure_allowed=False,
                external_id=key,
            ),
        )
        return SourceRef(
            source_id=made.source_id, version=made.version, scope=scope, availability="available"
        )

    async def step(self, task: WorkTask, step: WorkStep) -> Recorded | None:
        """A confirmed result: files written through their owner by this attempt."""
        if step.status != "succeeded" or not step.written:
            return None
        scope = task.scope
        written = ", ".join(f"{ref.artifact_id} v{ref.version}" for ref in step.written)
        text = (
            f"Работа над задачей: {task.goal[:500]}\n"
            f"Действие ({step.capability_id}): {step.summary[:1500]}\n"
            f"Записано владельцем файлов: {written}"
        )
        read = tuple(step.read_sources)
        observed = Provenance.SIMULATION if scope.memory_mode == "test" else Provenance.OBSERVATION
        # The basis may go to the model later only if everything it was made from may.
        own = await self._source(
            scope,
            step.attempt_id,
            "workplace-result",
            observed,
            text,
            await self._transferable(scope, read),
        )
        episode, _ = await self.memory.record_episode(
            _command(scope, "experience-episode-" + step.attempt_id),
            EpisodeDraft(
                sources=(*read, own),
                experience_source_ids=(own.source_id,),
                participant_ids=(),
                choice=f"Использовала возможность {step.capability_id}.",
                actual_outcome=step.summary[:2000],
                operation_id="work-" + step.attempt_id,
                provenance="simulation" if scope.memory_mode == "test" else "observation",
            ),
        )
        return Recorded(own, str(episode))

    async def conclusion(self, task: WorkTask, attempt_id: str) -> Recorded | None:
        """The agent's own conclusion: an interpretation, never an episode."""
        if task.state != "done" or task.outcome is None:
            return None
        own = await self._source(
            task.scope,
            attempt_id + ".conclusion",
            "workplace-conclusion",
            Provenance.INTERPRETATION,
            f"Мой вывод по задаче «{task.goal[:500]}»: {task.outcome[:2000]}",
            True,
        )
        return Recorded(own, None)
