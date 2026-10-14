"""Provider wrappers for the evaluation: a recorder (usage, latency, faults, budget), a dry model.

``DryModel`` is a deterministic stand-in with canned generic replies; running it proves the
harness, the platform pipeline and the report, and says nothing about a real model. The recorder
wraps *any* provider, so the same code path measures a real one once it is authorised.
"""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from time import perf_counter

from ai_agent_contracts.llm import TokenUsage

from ai_agent_application.llm import (
    Delta,
    Failure,
    Finished,
    LLMProvider,
    ProviderCall,
    ProviderError,
    Started,
    UsageReported,
)
from ai_agent_application.llm.model import ProviderEvent

from .budget import BudgetGuard
from .model import Call, Trace

DRY_MODEL_ID = "dry-run-stand-in"
CANNED = (
    "Понимаю. Расскажи ещё.",
    "Хорошо, давай попробуем.",
    "Я подумаю об этом.",
    "Интересно, продолжай.",
    "Здорово, что ты об этом сказал.",
)


class DryModel:
    """Deterministic canned replies, local token estimate. Never used for quality conclusions."""

    def stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
        return self._play(call)

    async def _play(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
        if call.assignment.role_id != "character":
            raise ProviderError(Failure.REJECTED)  # only the dialogue role is exercised here
        prompt = "\n".join(m.content for m in call.messages)
        digest = hashlib.sha256(call.messages[-1].content.encode("utf-8")).digest()
        text = json.dumps({"reply": CANNED[digest[0] % len(CANNED)]}, ensure_ascii=False)
        yield Started(DRY_MODEL_ID)
        for start in range(0, len(text), 16):
            yield Delta(text[start : start + 16])
            await asyncio.sleep(0)
        yield UsageReported(
            TokenUsage(
                input_tokens=-(-len(prompt) // 4),
                output_tokens=-(-len(text) // 4),
                source="local_estimate",
                completeness="complete",
            )
        )
        yield Finished("stop")


class Recorder:
    """Wraps a provider: records every call into the current trace, applies faults and budget."""

    def __init__(self, inner: LLMProvider, guard: BudgetGuard) -> None:
        self.inner, self.guard = inner, guard
        self.trace = Trace()
        self._faults: list[Failure] = []

    def inject(self, failure: Failure, calls: int) -> None:
        self._faults.extend([failure] * calls)

    def reset(self, trace: Trace) -> None:
        self.trace = trace
        self._faults.clear()

    def stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
        return self._record(call)

    async def _record(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
        prompt = "\n".join(m.content for m in call.messages)
        record = Call(role=call.assignment.role_id, model=call.assignment.model_id, prompt=prompt)
        self.trace.calls.append(record)
        if self._faults:  # injected before any network use: nothing is billed
            record.failure = self._faults.pop(0).value
            raise ProviderError(Failure(record.failure))
        if not self.guard.admit(len(prompt)):
            record.failure = Failure.NOT_ADMITTED.value
            raise ProviderError(Failure.NOT_ADMITTED)
        began = perf_counter()
        usage: TokenUsage | None = None
        out_chars = 0
        try:
            async for event in self.inner.stream(call):
                if isinstance(event, Started):
                    record.started = True
                elif isinstance(event, Delta):
                    out_chars += len(event.text)
                elif isinstance(event, UsageReported):
                    usage = event.usage  # the latest report replaces earlier ones
                yield event
        except ProviderError as error:
            record.failure = error.failure.value
            raise
        finally:
            record.latency_ms = (perf_counter() - began) * 1000
            if record.started:  # a started completion may be billed even when it was cut short
                self.guard.charge(usage, len(prompt), out_chars)
                if usage is not None:
                    record.input_tokens = usage.input_tokens
                    record.output_tokens = usage.output_tokens
                    record.usage_source = usage.source
