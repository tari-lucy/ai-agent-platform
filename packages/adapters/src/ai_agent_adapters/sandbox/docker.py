"""Bounded Docker CLI launcher over accepted AW-00 TaskRunner; no host shell."""

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any, Literal, cast
from uuid import uuid4

from ai_agent_application.agent_workplace.ports import (
    AttemptContext,
    ModelGateway,
    ReleaseGuard,
    RunnerOutcome,
    RunnerOutput,
    RunnerTask,
    WorkAdmission,
    WorkspaceFiles,
    WorkspaceGateway,
)
from ai_agent_application.workspace_files.guard import current
from ai_agent_contracts.agent_workplace import WorkAttempt, logical_path
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode

from .entry import ENTRY
from .helper_protocol import helper_request
from .model_protocol import model_request, wire_calls, workspace_request
from .policy import RunnerTemplate, create_args

type Authorize = Callable[[WorkAdmission, str], Awaitable[None]]
type Lifecycle = Callable[
    [WorkAdmission, str, Literal["register", "removed", "unknown"]], Awaitable[None]
]


async def cli(*args: str, data: bytes = b"", cap: int = 65536, timeout: float = 10) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert process.stdout is not None and process.stdin is not None

    async def feed() -> None:
        assert process.stdin is not None
        process.stdin.write(data)
        try:
            await process.stdin.drain()
        except BrokenPipeError, ConnectionResetError:
            pass
        finally:
            process.stdin.close()

    sender = asyncio.create_task(feed())
    try:
        async with asyncio.timeout(timeout):
            output = bytearray()
            while True:
                chunk = await process.stdout.read(min(65536, cap + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > cap:
                    raise OSError("RUNNER_WIRE_LIMIT")
            await sender
            await process.wait()
            if process.returncode != 0:
                raise OSError("RUNNER_DOCKER_FAILED")
            return bytes(output)
    finally:
        sender.cancel()
        with contextlib.suppress(BaseException):
            await sender
        if process.returncode is None:
            process.kill()
            await process.wait()


class DockerTaskRunner:
    def __init__(
        self,
        templates: tuple[RunnerTemplate, ...],
        files: WorkspaceFiles,
        attempts: AttemptContext,
        guard: ReleaseGuard,
        *,
        authorize: Authorize,
        lifecycle: Lifecycle,
        cache_seconds: float = 30,
        models: ModelGateway | None = None,
        workspace: WorkspaceGateway | None = None,
        helpers: WorkspaceGateway | None = None,
    ) -> None:
        if not templates or len({t.id for t in templates}) != len(templates):
            raise ValueError("RUNNER_TEMPLATES_INVALID")
        if not 0 < cache_seconds <= 300:
            raise ValueError("RUNNER_CACHE_INVALID")
        self.templates = {item.id: item for item in templates}
        self.files, self.attempts, self.guard = files, attempts, guard
        self.authorize, self.lifecycle, self.cache_seconds = authorize, lifecycle, cache_seconds
        self.models = models
        self.workspace = workspace
        self.helpers = helpers
        self._active: tuple[WorkAdmission, str, asyncio.Event] | None = None
        self._cache: tuple[WorkAdmission, WorkAttempt, float, dict[RunnerOutput, bytes]] | None = (
            None
        )
        self._expiry: asyncio.TimerHandle | None = None
        self._idle = asyncio.Event()
        self._idle.set()
        self._residual: str | None = None
        self._busy = False
        self._generation = 0

    def _clear_cache(self) -> None:
        self._cache = None
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None

    async def _attach(
        self,
        name: str,
        payload: bytes,
        task: RunnerTask,
        admission: WorkAdmission,
        attempt: WorkAttempt,
        template: RunnerTemplate,
        wire_cap: int,
    ) -> bytes:
        frame_cap = max(wire_cap, template.max_prompt_chars * 6 + 8192)
        process = await asyncio.create_subprocess_exec(
            "docker",
            "start",
            "--attach",
            "--interactive",
            name,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=frame_cap + 1,
        )
        assert process.stdin is not None and process.stdout is not None
        final: bytes | None = None
        calls = 0
        workspace_calls = 0
        helper_calls = 0
        try:
            async with asyncio.timeout(task.limits.wall_seconds + 3):
                process.stdin.write(payload + b"\n")
                await process.stdin.drain()
                while raw := await process.stdout.readline():
                    if len(raw) > frame_cap or final is not None:
                        raise ValueError("RUNNER_FRAME_INVALID")
                    row = json.loads(raw)
                    if row.get("kind") == "result":
                        if len(raw) > wire_cap:
                            raise ValueError("RUNNER_FRAME_INVALID")
                        final = raw
                        process.stdin.close()
                        continue
                    if row.get("kind") == "helper":
                        answer: dict[str, Any] = {
                            "status": "denied",
                            "code": "RESOURCE_BUSY"
                            if template.max_helper_calls > 0
                            and helper_calls >= template.max_helper_calls
                            else "CAPABILITY_UNAVAILABLE",
                        }
                        if (
                            task.network == "gateway"
                            and self.helpers is not None
                            and helper_calls < template.max_helper_calls
                        ):
                            helper_calls += 1
                            request = helper_request(row)
                            await self._verify(admission, attempt)
                            try:
                                text = await self.helpers.call(admission, request)
                                await self._verify(admission, attempt)
                                if not isinstance(text, str) or len(text.encode()) > min(
                                    16000, task.limits.output_bytes
                                ):
                                    raise ValueError("RUNNER_HELPER_OUTPUT_LIMIT")
                                answer = {"status": "completed", "text": text}
                            except ContractError as error:
                                answer = {"status": "denied", "code": error.error.code.value}
                        process.stdin.write(json.dumps(answer).encode() + b"\n")
                        await process.stdin.drain()
                        continue
                    if row.get("kind") == "workspace":
                        answer = {
                            "status": "denied",
                            "code": "RESOURCE_BUSY"
                            if template.max_workspace_calls > 0
                            and workspace_calls >= template.max_workspace_calls
                            else "CAPABILITY_UNAVAILABLE",
                        }
                        if (
                            task.network == "gateway"
                            and self.workspace is not None
                            and (workspace_calls < template.max_workspace_calls)
                        ):
                            workspace_calls += 1
                            request = workspace_request(row)
                            await self._verify(admission, attempt)
                            try:
                                text = await self.workspace.call(admission, request)
                                await self._verify(admission, attempt)
                                if not isinstance(text, str) or len(text.encode()) > min(
                                    16000, task.limits.output_bytes
                                ):
                                    raise ValueError("RUNNER_WORKSPACE_OUTPUT_LIMIT")
                                answer = {"status": "completed", "text": text}
                            except ContractError as error:
                                answer = {"status": "denied", "code": error.error.code.value}
                        process.stdin.write(json.dumps(answer).encode() + b"\n")
                        await process.stdin.drain()
                        continue
                    if row.get("kind") != "model" or task.network != "gateway":
                        raise ValueError("RUNNER_FRAME_INVALID")
                    answer = {"status": "not_admitted", "text": ""}
                    if calls < template.max_model_calls:
                        calls += 1
                        await self._verify(admission, attempt)
                        await self.guard.verify(
                            admission, attempt.inputs, attempt.input_files, "api_transfer"
                        )
                        if self.models is None or template.model_role_id is None:
                            raise ValueError("RUNNER_MODEL_REQUEST_INVALID")
                        reply = await self.models.generate(
                            admission,
                            model_request(
                                row,
                                role=template.model_role_id,
                                max_tokens=template.max_model_tokens,
                                max_prompt=template.max_prompt_chars,
                                call_id=f"sandbox-{calls}",
                            ),
                        )
                        if reply.status != "completed":
                            # Preserve the terminal status; this is not an upstream rate limit.
                            answer = {"status": reply.status, "text": ""}
                        await self._verify(admission, attempt)
                        await self.guard.verify(
                            admission, attempt.inputs, attempt.input_files, "api_transfer"
                        )
                        if reply.status == "completed":
                            if len(reply.text.encode()) > task.limits.output_bytes:
                                raise ValueError("RUNNER_MODEL_OUTPUT_LIMIT")
                            answer = {
                                "status": "completed",
                                "text": reply.text,
                                "tool_calls": wire_calls(reply.tool_calls),
                            }
                            if len(json.dumps(answer).encode()) > task.limits.output_bytes:
                                raise ValueError("RUNNER_MODEL_OUTPUT_LIMIT")
                    else:
                        logging.getLogger(__name__).warning(
                            "Sandbox model call refused: MODEL_CALL_LIMIT"
                        )
                    process.stdin.write(json.dumps(answer).encode() + b"\n")
                    await process.stdin.drain()
                await process.wait()
                if process.returncode != 0 or final is None:
                    raise ValueError("RUNNER_EXIT_UNKNOWN")
                return final
        finally:
            process.stdin.close()
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def _verify(self, admission: WorkAdmission, attempt: WorkAttempt) -> None:
        current(admission)
        await self.authorize(admission, attempt.capability_id)
        await self.guard.verify(admission, attempt.inputs, attempt.input_files, "private")
        current(admission)

    async def _attempt(self, admission: WorkAdmission) -> WorkAttempt:
        current(admission)
        attempt = await self.attempts.attempt(admission.scope, admission.attempt_id)
        if (
            attempt is None
            or attempt.scope != admission.scope
            or attempt.freshness != admission.freshness
            or attempt.operation_id != admission.operation_id
            or attempt.state not in {"prepared", "submitted"}
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        await self._verify(admission, attempt)
        return attempt

    @staticmethod
    async def _image(template: RunnerTemplate) -> None:
        info = json.loads(await cli("image", "inspect", template.image_id, cap=65536))[0]
        config = info["Config"]
        if info["Os"] != "linux" or config.get("Volumes"):
            raise OSError("RUNNER_IMAGE_UNSAFE")
        # Never inherit arbitrary image env (including unexpected credential names).
        # Image contents themselves must still be trusted and pinned by setup.
        safe_env = {
            "PATH",
            "LANG",
            "LC_ALL",
            "PYTHON_VERSION",
            "PYTHON_SHA256",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHONUNBUFFERED",
            "HF_HUB_OFFLINE",
            "TOKENIZERS_PARALLELISM",
            "NODE_VERSION",
            "YARN_VERSION",
            "UV_PYTHON_DOWNLOADS",
            "UV_NO_PROGRESS",
        }
        if any(value.split("=", 1)[0] not in safe_env for value in config.get("Env", ())):
            raise OSError("RUNNER_IMAGE_UNSAFE")

    @staticmethod
    async def _remove(name: str) -> bool:
        try:
            present = await cli(
                "ps",
                "--all",
                "--filter",
                "label=ai-agent-platform.instance=" + name,
                "--format",
                "{{.ID}}",
            )
            if not present.strip():
                return True
            info = json.loads(await cli("inspect", name, cap=65536))[0]
            if info["Config"]["Labels"].get("ai-agent-platform.instance") != name:
                return False
            await cli("rm", "--force", name)
            return True
        except OSError, TimeoutError, ValueError, KeyError, IndexError:
            return False

    def _decode(
        self, raw: bytes, task: RunnerTask
    ) -> tuple[RunnerOutcome, dict[RunnerOutput, bytes]]:
        row: Any = json.loads(raw)
        state = row["status"]
        if state not in {"succeeded", "failed", "timeout", "limit"}:
            raise ValueError("RUNNER_RESULT_INVALID")
        exit_code = row["exit_code"]
        if type(exit_code) is not int or state == "succeeded" and exit_code != 0:
            raise ValueError("RUNNER_RESULT_INVALID")
        stdout = base64.b64decode(row["stdout"], validate=True)
        total = len(stdout)
        files: dict[RunnerOutput, bytes] = {}
        names: set[str] = set()
        for item in row["files"]:
            name = logical_path(item["name"])
            data = base64.b64decode(item["content"], validate=True)
            if name in names or hashlib.sha256(data).hexdigest() != item["checksum"]:
                raise ValueError("RUNNER_RESULT_INVALID")
            names.add(name)
            total += len(data)
            files[RunnerOutput(name, item["checksum"], len(data))] = data
        if (
            total > task.limits.output_bytes
            or len(files) > self.templates[task.template_id].max_files
        ):
            raise ValueError("RUNNER_RESULT_LIMIT")
        if state != "succeeded" and files:
            raise ValueError("RUNNER_RESULT_INVALID")
        return RunnerOutcome(
            cast(Any, state), exit_code, tuple(files), stdout.decode("utf-8", "replace")
        ), files

    async def run(self, admission: WorkAdmission, task: RunnerTask) -> RunnerOutcome:
        if self._busy or self._residual is not None:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        self._busy = True
        self._idle.clear()
        try:
            return await self._run(admission, task)
        finally:
            self._active = None
            self._busy = False
            self._idle.set()

    async def _watch(
        self,
        attached: asyncio.Task[bytes],
        admission: WorkAdmission,
        attempt: WorkAttempt,
        stopped: asyncio.Event,
        deadline: float,
    ) -> RunnerOutcome | None:
        # A slow read-only proof is not evidence of an unknown container outcome.
        # Keep one proof in flight, while epoch/lease cancellation and the existing
        # total deadline remain independently observable. Action boundaries and
        # result release still require their own complete verification.
        proof: asyncio.Task[None] | None = None
        next_proof = 0.0
        interval = self.templates[attempt.capability_id].monitor_interval_seconds
        try:
            while not attached.done():
                try:
                    current(admission)
                    if stopped.is_set():
                        return RunnerOutcome("cancelled", None, (), "")
                    if proof is not None and proof.done():
                        await proof
                        proof = None
                        # The monitor does not need to saturate SQLite between IO
                        # boundaries. No proof is reused to authorize an action:
                        # model/tool/file/result boundaries still call _verify fresh.
                        next_proof = time.monotonic() + interval
                except ContractError:
                    return RunnerOutcome("cancelled", None, (), "")
                if time.monotonic() >= deadline:
                    return RunnerOutcome("timeout", None, (), "")
                if proof is None and time.monotonic() >= next_proof:
                    proof = asyncio.create_task(self._verify(admission, attempt))
                # Do not let an immediately completed proof create a busy loop
                # or repeatedly contend with the operation owner's SQLite lock.
                await asyncio.wait({attached}, timeout=0.1)
            return None
        finally:
            if proof is not None:
                proof.cancel()
                await asyncio.gather(proof, return_exceptions=True)

    async def _run(self, admission: WorkAdmission, task: RunnerTask) -> RunnerOutcome:
        template = self.templates.get(task.template_id)
        if template is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        template.check(task)
        if task.network == "gateway" and self.models is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        if self._active is not None or self._residual is not None:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        name = "aw-task-" + uuid4().hex
        stopped = asyncio.Event()
        self._active = admission, name, stopped
        generation = self._generation
        self._clear_cache()
        registered = False
        cleanup = False
        result = RunnerOutcome("unknown", None, (), "")
        contents: dict[RunnerOutput, bytes] = {}
        attached: asyncio.Task[bytes] | None = None
        try:
            attempt = await self._attempt(admission)
            if attempt.capability_id != template.id:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            if any(ref not in attempt.input_files for ref in task.inputs):
                raise ContractError(ErrorCode.ACCESS_DENIED)
            inputs = []
            total = 0
            for ref in task.inputs:
                entry, data = await self.files.read(admission, ref.artifact_id, ref.version)
                if entry.reference != ref:
                    raise ContractError(ErrorCode.STALE_CONTEXT)
                total += len(data)
                if total > min(template.max_input_bytes, task.limits.disk_bytes // 2):
                    raise ContractError(ErrorCode.RESOURCE_BUSY)
                inputs.append(
                    {
                        "id": ref.artifact_id,
                        "version": ref.version,
                        "content": base64.b64encode(data).decode("ascii"),
                    }
                )
            await self._image(template)
            await self._verify(admission, attempt)
            await self.lifecycle(admission, name, "register")
            registered = True
            args = create_args(template, task, name, ENTRY)
            args[1:1] = ["--label", "ai-agent-platform.instance=" + name]
            await cli(*args)
            await self._verify(admission, attempt)
            payload = json.dumps(
                {
                    "inputs": inputs,
                    "command": task.command,
                    "limits": asdict(task.limits),
                    "max_files": template.max_files,
                    "gateway": task.network == "gateway",
                    "max_prompt_chars": template.max_prompt_chars,
                    "max_model_tokens": template.max_model_tokens,
                    "workspace": self.workspace is not None and template.max_workspace_calls > 0,
                    "helper": self.helpers is not None and template.max_helper_calls > 0,
                }
            ).encode()
            wire_cap = task.limits.output_bytes * 2 + template.max_files * 1024 + 4096
            attached = asyncio.create_task(
                self._attach(name, payload, task, admission, attempt, template, wire_cap)
            )
            deadline = time.monotonic() + task.limits.wall_seconds + 2
            interrupted = await self._watch(attached, admission, attempt, stopped, deadline)
            if interrupted is not None:
                result = interrupted
            else:
                result, contents = self._decode(await attached, task)
                state = json.loads(await cli("inspect", "--format", "{{json .State}}", name))
                if state["Running"] or state["OOMKilled"] or state["ExitCode"] != 0:
                    result, contents = (
                        RunnerOutcome("limit" if state["OOMKilled"] else "unknown", None, (), ""),
                        {},
                    )
                await self._verify(admission, attempt)
        except OSError, ValueError, KeyError, IndexError, TypeError, TimeoutError:
            result, contents = RunnerOutcome("unknown", None, (), ""), {}
        finally:
            if attached is not None:
                attached.cancel()
            if registered:
                removal = asyncio.create_task(self._remove(name))
                try:
                    cleanup = await asyncio.shield(removal)
                except asyncio.CancelledError:
                    cleanup = await removal
                    if not cleanup:
                        self._residual = name
                    await self.lifecycle(admission, name, "removed" if cleanup else "unknown")
                    raise
                if not cleanup:
                    self._residual = name
                await self.lifecycle(admission, name, "removed" if cleanup else "unknown")
            # Stop the container before awaiting any late technical LLM receipt.
            if attached is not None:
                with contextlib.suppress(BaseException):
                    await attached
        if not cleanup:
            return RunnerOutcome("unknown", None, (), "")
        try:
            await self._verify(admission, attempt)
        except ContractError:
            return RunnerOutcome("cancelled", None, (), "")
        if generation != self._generation:
            return RunnerOutcome("cancelled", None, (), "")
        if result.status == "succeeded":
            self._cache = admission, attempt, time.monotonic() + self.cache_seconds, contents
            self._expiry = asyncio.get_running_loop().call_later(
                self.cache_seconds, self._clear_cache
            )
        return result

    async def staged(self, admission: WorkAdmission, output: RunnerOutput) -> bytes:
        cached = self._cache
        if cached is None or time.monotonic() >= cached[2]:
            self._clear_cache()
            raise ContractError(ErrorCode.EXPIRED)
        original, attempt, _, files = cached
        if original != admission or output not in files:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        await self._verify(admission, attempt)
        if self._cache is not cached:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        return files[output]

    async def cancel(self, admission: WorkAdmission) -> None:
        active = self._active
        if active is not None and active[0] != admission:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if active is not None and active[0] == admission:
            self._generation += 1
            active[2].set()
        if self._cache is not None and self._cache[0] == admission:
            self._clear_cache()
        await self._settled()

    async def _settled(self) -> None:
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=25)
        except TimeoutError:
            raise ContractError(ErrorCode.OUTCOME_UNKNOWN) from None
        if self._residual is not None:
            raise ContractError(ErrorCode.OUTCOME_UNKNOWN)

    async def reconcile(self) -> None:
        """Trusted JobOwner recovery, never a retry of the command itself."""
        if self._busy:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        if self._residual is not None:
            if not await self._remove(self._residual):
                raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
            self._residual = None

    async def purge(self) -> None:
        """Trusted lifecycle hook; the existing retention owner invokes it, never the agent."""
        self._clear_cache()
        self._generation += 1
        if self._active is not None:
            self._active[2].set()
        await self._settled()
