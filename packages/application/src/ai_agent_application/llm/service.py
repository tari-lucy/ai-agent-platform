"""LLM gateway: bounded retry, visible fallback, cancellation, usage honesty, strict results.

The gateway never executes anything. A generation is ``completed`` only after the provider's own
terminal event, and only then are ``text`` and ``parsed`` set; a broken, truncated, cancelled or
late-after-STOP stream leaves ``preview`` for the operator and no result. Every provider call is
reported once to the attempt sink, including cancelled ones; a missing usage stays unknown.
"""

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal

from ai_agent_contracts.llm import TokenUsage
from pydantic import BaseModel, ValidationError

from .model import (
    FALLBACK_ELIGIBLE,
    MAX_OUTPUT_CHARS,
    RETRYABLE,
    UNKNOWN_USAGE,
    AttemptReport,
    AttemptStatus,
    Delta,
    Failure,
    Finished,
    Generation,
    GenerationRequest,
    ProviderCall,
    ProviderError,
    ResolvedAssignment,
    Started,
    ToolCallDelta,
    UsageReported,
)
from .ports import Admission, AttemptJournal, AttemptSink, LLMProvider, ProviderFactory
from .tools import ToolBuffer, ToolCall, ToolProtocolError

MAX_RETRY_PAUSE_SECONDS = 30.0
MAX_RESULT_BYTES = 256_000

type DeltaSink = Callable[[str], Awaitable[None]]
type Clock = Callable[[], datetime]
type Sleep = Callable[[float], Awaitable[None]]


class _End:
    """Marker: the provider stream finished without raising."""


class _Cancelled:
    """Marker: the caller's cancel event won the race."""


def _duplicate_free(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _nonfinite(_: str) -> None:
    raise ValueError("NONFINITE_JSON")


def parse_result(text: str, model: type[BaseModel]) -> BaseModel | None:
    """Strict whole-document parse: one JSON value, no fences, no duplicate keys, closed schema."""
    if len(text.encode("utf-8", "replace")) > MAX_RESULT_BYTES:
        return None
    try:
        data = json.loads(text, object_pairs_hook=_duplicate_free, parse_constant=_nonfinite)
        return model.model_validate(data)
    except ValueError, RecursionError, ValidationError:
        return None


class _Outcome:
    def __init__(self) -> None:
        self.status: AttemptStatus | None = None
        self.failure: Failure | None = None
        self.text = ""
        self.reported_model: str | None = None
        self.usage: TokenUsage = UNKNOWN_USAGE
        self.retry_after: float | None = None
        self.tool_buffer = ToolBuffer()
        self.tool_calls: tuple[ToolCall, ...] = ()
        self.tools_rejected = False  # a finished tool proposal failed validation, unexecuted


class LLMGateway:
    def __init__(
        self,
        providers: ProviderFactory,
        resolve: Callable[[str], ResolvedAssignment],
        *,
        sink: AttemptSink | None = None,
        journal: AttemptJournal | None = None,
        admission: Admission | None = None,
        clock: Clock | None = None,
        max_attempts: int = 2,
        base_delay: float = 0.5,
        sleep: Sleep = asyncio.sleep,
        jitter: Callable[[], float] | None = None,
        retryable: frozenset[Failure] = RETRYABLE,
    ) -> None:
        """``retryable``: failures worth one more attempt when nothing was streamed yet."""
        if not 1 <= max_attempts <= 2 or not retryable <= RETRYABLE:
            raise ValueError("MAX_ATTEMPTS_BOUNDS")
        self._providers = providers
        self._resolve = resolve
        self._sink, self._journal = sink, journal
        self._admission = admission
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_attempts = max_attempts
        self._retryable = retryable
        self._base_delay = base_delay
        self._sleep = sleep
        self._jitter = jitter or (lambda: random.uniform(0.75, 1.25))

    async def generate(
        self,
        request: GenerationRequest,
        cancel: asyncio.Event | None = None,
        on_delta: DeltaSink | None = None,
    ) -> Generation:
        try:
            chain = self._chain(self._resolve(request.role_id))
        except ProviderError as error:
            return self._finish(request, "failed", error.failure, "", None, [], False, None)
        attempts: list[AttemptReport] = []
        preview = ""
        for position, assignment in enumerate(chain):
            if request.max_output_tokens is not None:
                assignment = replace(
                    assignment,
                    parameters=assignment.parameters.model_copy(
                        update={
                            "max_output_tokens": min(
                                assignment.parameters.max_output_tokens, request.max_output_tokens
                            ),
                        }
                    ),
                )
            for number in range(self._max_attempts):
                if self._stopped(cancel):
                    return self._cancelled(request, preview, attempts, position, assignment)
                outcome, report = await self._attempt(
                    request, assignment, len(attempts) + 1, cancel, on_delta
                )
                attempts.append(report)
                preview = outcome.text
                if outcome.status == "completed":
                    return self._complete(request, outcome, attempts, position, assignment, cancel)
                failure = outcome.failure
                if failure is Failure.CANCELLED or failure is Failure.NOT_ADMITTED:
                    return self._finish(
                        request,
                        "cancelled",
                        failure,
                        preview,
                        None,
                        attempts,
                        position > 0,
                        assignment.assignment_id,
                    )
                # A rejected tool proposal was never executed or shown; its text was shown
                # only through on_delta. Asking again costs one bounded attempt, not a turn.
                rejected = outcome.tools_rejected and (on_delta is None or not outcome.text)
                if (
                    (
                        rejected
                        or failure in self._retryable
                        and not outcome.text
                        and not outcome.tool_buffer.parts
                    )
                    and number + 1 < self._max_attempts
                ):
                    if not await self._pause(self._delay(number, outcome.retry_after), cancel):
                        return self._cancelled(request, preview, attempts, position, assignment)
                    continue
                break
            assert outcome.failure is not None
            if (
                outcome.failure in FALLBACK_ELIGIBLE
                and not outcome.text
                and not outcome.tool_buffer.parts
                and position + 1 < len(chain)
            ):
                continue
            status: Literal["failed", "incomplete"] = (
                "incomplete" if outcome.text or outcome.tool_buffer.parts else "failed"
            )
            return self._finish(
                request,
                status,
                outcome.failure,
                preview,
                None,
                attempts,
                position > 0,
                assignment.assignment_id,
            )
        raise AssertionError("unreachable: the chain always ends in a terminal return")

    # -- helpers ---------------------------------------------------------------------------

    @staticmethod
    def _chain(first: ResolvedAssignment) -> list[ResolvedAssignment]:
        chain: list[ResolvedAssignment] = []
        seen: set[str] = set()
        current: ResolvedAssignment | None = first
        while current is not None:
            if current.assignment_id in seen:
                raise ProviderError(Failure.UNSUPPORTED)
            seen.add(current.assignment_id)
            chain.append(current)
            current = current.fallback
        if len(chain) > 3:
            raise ProviderError(Failure.UNSUPPORTED)
        return chain

    def _stopped(self, cancel: asyncio.Event | None) -> bool:
        return (cancel is not None and cancel.is_set()) or (
            self._admission is not None and not self._admission()
        )

    def _delay(self, number: int, retry_after: float | None) -> float:
        base = retry_after if retry_after is not None else self._base_delay * (2**number)
        return min(MAX_RETRY_PAUSE_SECONDS, max(0.0, base)) * self._jitter()

    async def _pause(self, seconds: float, cancel: asyncio.Event | None) -> bool:
        """Sleep unless cancelled; False when the cancel event ended the wait."""
        task = asyncio.ensure_future(self._sleep(seconds))
        return await self._race(task, cancel) is not _Cancelled

    @staticmethod
    async def _race[T](task: asyncio.Future[T], cancel: asyncio.Event | None) -> Any:
        if cancel is None:
            return await task
        waiter = asyncio.ensure_future(cancel.wait())
        try:
            pending: set[asyncio.Future[Any]] = {task, waiter}
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            task.cancel()
            raise
        finally:
            waiter.cancel()
        if cancel.is_set() or not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            return _Cancelled
        return task.result()

    async def _next(self, events: Any, cancel: asyncio.Event | None) -> Any:
        async def step() -> Any:
            try:
                return await anext(events)
            except StopAsyncIteration:
                return _End

        return await self._race(asyncio.ensure_future(step()), cancel)

    async def _attempt(
        self,
        request: GenerationRequest,
        assignment: ResolvedAssignment,
        number: int,
        cancel: asyncio.Event | None,
        on_delta: DeltaSink | None,
    ) -> tuple[_Outcome, AttemptReport]:
        outcome = _Outcome()
        started_at = self._clock()
        events: Any = None
        journal_started = False
        try:
            if self._journal is not None:
                before = AttemptReport(
                    operation_id=request.operation_id,
                    attempt_id=f"{request.operation_id}.a{number}",
                    role_id=request.role_id,
                    assignment_id=assignment.assignment_id,
                    assignment_version=assignment.assignment_version,
                    connection_id=assignment.connection_id,
                    requested_model_id=assignment.model_id,
                    reported_model_id=None,
                    status="unknown",
                    failure=None,
                    started_at=started_at,
                    completed_at=started_at,
                    usage=UNKNOWN_USAGE,
                    delivered_chars=0,
                )
                try:
                    await self._journal.begin(before)
                except Exception:
                    raise ProviderError(Failure.NOT_ADMITTED) from None
                journal_started = True
            if self._stopped(cancel):
                raise ProviderError(Failure.NOT_ADMITTED)
            provider: LLMProvider = self._providers(assignment.adapter_id)
            events = provider.stream(
                ProviderCall(
                    assignment=assignment,
                    messages=request.messages,
                    tools=request.tools,
                    result_model=request.result_model,
                )
            )
            async with asyncio.timeout(assignment.parameters.timeout_seconds):
                await self._consume(events, outcome, cancel, on_delta, request)
        except ProviderError as error:
            outcome.failure = error.failure
            outcome.retry_after = error.retry_after
        except KeyError:
            outcome.failure = Failure.UNSUPPORTED
        except TimeoutError:
            outcome.failure = Failure.TIMEOUT
        except Exception:  # an adapter bug must not leak its message into results or logs
            outcome.failure = Failure.PROTOCOL
        finally:
            # also reached on task cancellation: close the stream and still report the attempt
            if events is not None and hasattr(events, "aclose"):
                with contextlib.suppress(BaseException):
                    await asyncio.shield(events.aclose())
            self._settle(outcome)
            if outcome.failure is not None:
                # Enum values only. Do not log operation/model ids, previews or exceptions.
                logging.getLogger(__name__).warning(
                    "LLM attempt ended: status=%s failure=%s",
                    outcome.status,
                    outcome.failure.value,
                )
            report = AttemptReport(
                operation_id=request.operation_id,
                attempt_id=f"{request.operation_id}.a{number}",
                role_id=request.role_id,
                assignment_id=assignment.assignment_id,
                assignment_version=assignment.assignment_version,
                connection_id=assignment.connection_id,
                requested_model_id=assignment.model_id,
                reported_model_id=outcome.reported_model,
                status=outcome.status or "cancelled",
                failure=outcome.failure,
                started_at=started_at,
                completed_at=max(self._clock(), started_at),
                usage=outcome.usage,
                delivered_chars=len(outcome.text),
            )
            if self._journal is not None and journal_started:
                technical = asyncio.create_task(self._journal.finish(report))
                try:
                    await asyncio.shield(technical)
                except asyncio.CancelledError:
                    await technical
                    raise
            if self._sink is not None:
                self._sink(report)
        return outcome, report

    async def _consume(
        self,
        events: Any,
        outcome: _Outcome,
        cancel: asyncio.Event | None,
        on_delta: DeltaSink | None,
        request: GenerationRequest,
    ) -> None:
        reason: str | None = None
        outcome.status = None
        while True:
            if self._stopped(cancel):
                outcome.failure = (
                    Failure.CANCELLED
                    if cancel is not None and cancel.is_set()
                    else Failure.NOT_ADMITTED
                )
                return
            event = await self._next(events, cancel)
            if event is _Cancelled:
                outcome.failure = Failure.CANCELLED
                return
            if event is _End:
                break
            if self._stopped(cancel):  # a late event after STOP is dropped, not delivered
                outcome.failure = (
                    Failure.CANCELLED
                    if cancel is not None and cancel.is_set()
                    else Failure.NOT_ADMITTED
                )
                return
            if isinstance(event, Started):
                outcome.reported_model = outcome.reported_model or event.reported_model
            elif isinstance(event, Delta):
                if reason is not None:
                    raise ProviderError(Failure.PROTOCOL)  # text after the terminal event
                if len(outcome.text) + len(event.text) > MAX_OUTPUT_CHARS:
                    raise ProviderError(Failure.PROTOCOL)
                outcome.text += event.text
                if on_delta is not None:
                    await on_delta(event.text)
            elif isinstance(event, Finished):
                if reason is not None:
                    raise ProviderError(Failure.PROTOCOL)
                reason = event.reason
            elif isinstance(event, ToolCallDelta):
                if not request.tools:
                    raise ProviderError(Failure.UNSUPPORTED)
                if reason is not None:
                    raise ProviderError(Failure.PROTOCOL)
                try:
                    outcome.tool_buffer.add(event.index, event.id, event.name, event.arguments)
                except ValueError:
                    raise ProviderError(Failure.PROTOCOL) from None
            elif isinstance(event, UsageReported):
                outcome.usage = event.usage  # replaces: a late final usage is never added on top
        if reason is None:
            outcome.failure = Failure.TRANSIENT  # stream ended without a terminal event
            return
        if reason == "tool_calls" and request.tools:
            try:
                outcome.tool_calls = outcome.tool_buffer.finish(
                    {tool.name for tool in request.tools}
                )
            except ToolProtocolError as error:
                logging.getLogger(__name__).warning("LLM tool rejected: %s", error.code.value)
                outcome.tools_rejected = True
                raise ProviderError(Failure.MALFORMED_RESULT) from None
            except ValueError, RecursionError:
                # Every ToolProtocolError is constructed from a closed enum. No remote text.
                # The fallback code covers unexpected validation failures without their input.
                logging.getLogger(__name__).warning("LLM tool rejected: TOOL_CALL_INVALID")
                outcome.tools_rejected = True
                raise ProviderError(Failure.MALFORMED_RESULT) from None
            outcome.status = "completed"
            return
        if outcome.tool_buffer.parts:
            logging.getLogger(__name__).warning("LLM tool rejected: TOOL_TERMINAL_MISMATCH")
            raise ProviderError(Failure.PROTOCOL)
        outcome.failure = {
            "stop": None,
            "length": Failure.TRUNCATED,
            "content_filter": Failure.FILTERED,
            "tool_calls": Failure.UNSUPPORTED,
            "other": Failure.PROTOCOL,
        }[reason]
        if outcome.failure is None and not outcome.text.strip():
            outcome.failure = Failure.MALFORMED_RESULT
        if outcome.failure is None:
            outcome.status = "completed"

    @staticmethod
    def _settle(outcome: _Outcome) -> None:
        """Map the failure of an attempt to the ledger status the provider's view justifies."""
        if outcome.status == "completed":
            return
        failure = outcome.failure
        if failure is None:  # left through task cancellation
            outcome.failure = failure = Failure.CANCELLED
        if failure in (Failure.CANCELLED, Failure.NOT_ADMITTED):
            outcome.status = "cancelled"
        elif outcome.text or outcome.tool_buffer.parts:
            outcome.status = "incomplete"
        elif failure in (Failure.TIMEOUT, Failure.TRANSIENT, Failure.PROTOCOL):
            outcome.status = "unknown"  # no terminal answer: cost and effect stay unknown
        else:
            outcome.status = "failed"

    def _complete(
        self,
        request: GenerationRequest,
        outcome: _Outcome,
        attempts: list[AttemptReport],
        position: int,
        assignment: ResolvedAssignment,
        cancel: asyncio.Event | None,
    ) -> Generation:
        used = position > 0
        if self._stopped(cancel):  # a result arriving after STOP has no effect
            return self._finish(
                request,
                "cancelled",
                Failure.NOT_ADMITTED,
                outcome.text,
                None,
                attempts,
                used,
                assignment.assignment_id,
            )
        parsed: BaseModel | None = None
        if request.result_model is not None:
            parsed = parse_result(outcome.text, request.result_model)
            if parsed is None:
                return self._finish(
                    request,
                    "failed",
                    Failure.MALFORMED_RESULT,
                    outcome.text,
                    None,
                    attempts,
                    used,
                    assignment.assignment_id,
                )
        return Generation(
            operation_id=request.operation_id,
            status="completed",
            failure=None,
            text=outcome.text,
            parsed=parsed,
            preview=outcome.text,
            attempts=tuple(attempts),
            used_fallback=used,
            assignment_id=assignment.assignment_id,
            tool_calls=outcome.tool_calls,
        )

    def _cancelled(
        self,
        request: GenerationRequest,
        preview: str,
        attempts: list[AttemptReport],
        position: int,
        assignment: ResolvedAssignment,
    ) -> Generation:
        return self._finish(
            request,
            "cancelled",
            Failure.CANCELLED,
            preview,
            None,
            attempts,
            position > 0,
            assignment.assignment_id,
        )

    @staticmethod
    def _finish(
        request: GenerationRequest,
        status: Literal["failed", "cancelled", "incomplete"],
        failure: Failure,
        preview: str,
        parsed: BaseModel | None,
        attempts: list[AttemptReport],
        used_fallback: bool,
        assignment_id: str | None,
    ) -> Generation:
        return Generation(
            operation_id=request.operation_id,
            status=status,
            failure=failure,
            text=None,
            parsed=parsed,
            preview=preview,
            attempts=tuple(attempts),
            used_fallback=used_fallback,
            assignment_id=assignment_id,
        )
