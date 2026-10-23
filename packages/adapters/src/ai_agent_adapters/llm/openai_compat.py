"""OpenAI-compatible Chat Completions streaming over HTTPX + httpx-sse (M2-04).

The adapter reads the endpoint and credential by the *names* held in the assignment from an
injected settings mapping; it never opens ``.env`` itself and never puts the endpoint, headers,
body or remote error text into an exception, event or log. Redirects are not followed, so the
credential cannot leave for another host. Tool fragments remain data, never executable actions.
"""

import json
import math
from collections.abc import AsyncIterator, Iterator, Mapping
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

import httpx
from ai_agent_application.llm import (
    Delta,
    Failure,
    Finished,
    ProviderCall,
    ProviderError,
    Started,
    UsageReported,
)
from ai_agent_application.llm.model import ChatMessage, ProviderEvent, ToolCallDelta
from ai_agent_application.llm.tools import MAX_TOOL_CALLS, object_json
from ai_agent_contracts.llm import TokenUsage
from httpx_sse import SSEError, aconnect_sse

ADAPTER_ID = "openai-compatible-chat"
MAX_EVENT_BYTES = 256_000
MAX_EVENTS = 50_000
_LOOPBACK_NAMES = frozenset({"localhost"})
_REASONS = {
    "stop": "stop",
    "length": "length",
    "content_filter": "content_filter",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
}


def checked_endpoint(value: str) -> str:
    """HTTPS (or plain HTTP to loopback for local tests) without user info, query or fragment."""
    try:
        parts = urlsplit(value.strip())
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        raise ProviderError(Failure.NOT_CONFIGURED) from None
    loopback = host in _LOOPBACK_NAMES
    if not loopback:
        try:
            loopback = ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if (
        parts.scheme not in ("https", "http")
        or (parts.scheme == "http" and not loopback)
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or (port is not None and not 0 < port < 65536)
    ):
        raise ProviderError(Failure.NOT_CONFIGURED)
    return value.strip().rstrip("/")


def _counter(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return int(value)


def usage_from(raw: Any) -> TokenUsage:
    """Provider usage as reported; absent or inconsistent numbers stay unknown, never zero."""
    if not isinstance(raw, dict):
        return TokenUsage(
            input_tokens=None, output_tokens=None, source="unknown", completeness="unknown"
        )
    input_tokens = _counter(raw.get("prompt_tokens"))
    output_tokens = _counter(raw.get("completion_tokens"))
    prompt_details = raw.get("prompt_tokens_details")
    completion_details = raw.get("completion_tokens_details")
    cached = (
        _counter(prompt_details.get("cached_tokens")) if isinstance(prompt_details, dict) else None
    )
    reasoning = (
        _counter(completion_details.get("reasoning_tokens"))
        if isinstance(completion_details, dict)
        else None
    )
    # Sub-categories sit inside their parent total (never added twice); impossible ones are unknown.
    if cached is not None and (input_tokens is None or cached > input_tokens):
        cached = None
    if reasoning is not None and (output_tokens is None or reasoning > output_tokens):
        reasoning = None
    if input_tokens is None and output_tokens is None:
        return TokenUsage(
            input_tokens=None, output_tokens=None, source="unknown", completeness="unknown"
        )
    complete = input_tokens is not None and output_tokens is not None
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
        reasoning_output_tokens=reasoning,
        source="provider",
        completeness="complete" if complete else "partial",
    )


def _retry_after(response: httpx.Response) -> float | None:
    try:
        value = float(response.headers.get("retry-after", ""))
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def failure_for_status(status: int) -> Failure:
    if status in (401, 403):
        return Failure.AUTH
    if status == 429:
        return Failure.RATE_LIMITED
    if status in (408, 409, 425) or status >= 500:
        return Failure.TRANSIENT
    return Failure.REJECTED


def _message(message: ChatMessage) -> dict[str, Any]:
    result: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_call_id is not None:
        result["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        result["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    return result


def _tool_events(raw: Any) -> list[ToolCallDelta]:
    if not isinstance(raw, list) or len(raw) > MAX_TOOL_CALLS:
        raise ProviderError(Failure.PROTOCOL)
    result = []
    for item in raw:
        if not isinstance(item, dict):
            raise ProviderError(Failure.PROTOCOL)
        index = item.get("index")
        function = item.get("function", {})
        if (
            type(index) is not int
            or not 0 <= index < MAX_TOOL_CALLS
            or item.get("type", "function") != "function"
            or not isinstance(function, dict)
        ):
            raise ProviderError(Failure.PROTOCOL)
        values = (item.get("id", ""), function.get("name", ""), function.get("arguments", ""))
        if not all(isinstance(value, str) for value in values):
            raise ProviderError(Failure.PROTOCOL)
        result.append(ToolCallDelta(index, *values))
    return result


class OpenAICompatibleProvider:
    """``LLMProvider`` for Chat Completions with ``stream=true`` and ``include_usage``."""

    def __init__(
        self,
        settings: Mapping[str, str],
        client: httpx.AsyncClient | None = None,
        *,
        stream_tools: bool = True,
    ) -> None:
        self._settings = settings
        self._client = client
        self.stream_tools = stream_tools

    def _http(self, timeout: float) -> tuple[httpx.AsyncClient, bool]:
        if self._client is not None:
            return self._client, False
        return httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=0),
        ), True

    def stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
        return self._stream(call)

    async def _stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
        assignment = call.assignment
        endpoint = self._settings.get(assignment.endpoint_setting, "")
        credential = self._settings.get(assignment.credential_setting, "")
        if not endpoint or not credential or any(c in credential for c in "\r\n"):
            raise ProviderError(Failure.NOT_CONFIGURED)
        url = checked_endpoint(endpoint) + "/chat/completions"
        parameters = assignment.parameters
        body: dict[str, Any] = {
            "model": assignment.model_id,
            "messages": [_message(message) for message in call.messages],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": parameters.max_output_tokens,
        }
        if call.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": object_json(tool.parameters_json),
                    },
                }
                for tool in call.tools
            ]
            if not self.stream_tools:
                # A complete tool_calls array has authoritative positions; do not guess
                # missing/offset streamed indices from an aggregator's content blocks.
                body["stream"] = False
                del body["stream_options"]
        if parameters.temperature is not None:
            body["temperature"] = parameters.temperature
        if parameters.top_p is not None:
            body["top_p"] = parameters.top_p
        headers = {"Authorization": f"Bearer {credential}", "Accept": "text/event-stream"}
        client, owned = self._http(parameters.timeout_seconds)
        try:
            if body["stream"] is False:
                headers["Accept"] = "application/json"
                async with client.stream("POST", url, json=body, headers=headers) as response:
                    if response.status_code != 200:
                        raise ProviderError(
                            failure_for_status(response.status_code), _retry_after(response)
                        )
                    if (
                        not response.headers.get("content-type", "")
                        .lower()
                        .startswith("application/json")
                    ):
                        raise ProviderError(Failure.PROTOCOL)
                    data = bytearray()
                    async for part in response.aiter_bytes():
                        data.extend(part)
                        if len(data) > MAX_EVENT_BYTES:
                            raise ProviderError(Failure.PROTOCOL)
                    try:
                        document = json.loads(data)
                    except ValueError, RecursionError:
                        raise ProviderError(Failure.PROTOCOL) from None
                    for event in self._complete_events(document):
                        yield event
                return
            async with aconnect_sse(client, "POST", url, json=body, headers=headers) as source:
                response = source.response
                if response.status_code != 200:
                    raise ProviderError(
                        failure_for_status(response.status_code), _retry_after(response)
                    )
                if (
                    not response.headers.get("content-type", "")
                    .lower()
                    .startswith("text/event-stream")
                ):
                    raise ProviderError(Failure.PROTOCOL)
                yield Started(None)
                async for event in self._events(source):
                    yield event
        except ProviderError:
            raise
        except httpx.TimeoutException:
            raise ProviderError(Failure.TIMEOUT) from None
        except httpx.TransportError, SSEError:
            raise ProviderError(Failure.TRANSIENT) from None
        except httpx.HTTPError:
            raise ProviderError(Failure.PROTOCOL) from None
        finally:
            if owned:
                await client.aclose()

    @staticmethod
    def _complete_events(document: Any) -> Iterator[ProviderEvent]:
        if not isinstance(document, dict) or "error" in document:
            raise ProviderError(Failure.PROTOCOL)
        choices = document.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ProviderError(Failure.PROTOCOL)
        choice = choices[0]
        message, reason = choice.get("message"), choice.get("finish_reason")
        if not isinstance(message, dict) or not isinstance(reason, str):
            raise ProviderError(Failure.PROTOCOL)
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ProviderError(Failure.PROTOCOL)
        model = document.get("model")
        yield Started(model[:128] if isinstance(model, str) and model else None)
        if content:
            yield Delta(content)
        if "tool_calls" in message:
            calls = message["tool_calls"]
            if not isinstance(calls, list) or any(not isinstance(c, dict) for c in calls):
                raise ProviderError(Failure.PROTOCOL)
            yield from _tool_events([dict(call, index=i) for i, call in enumerate(calls)])
        if document.get("usage") is not None:
            yield UsageReported(usage_from(document["usage"]))
        yield Finished(_REASONS.get(reason, "other"))  # type: ignore[arg-type]

    async def _events(self, source: Any) -> AsyncIterator[ProviderEvent]:
        count = 0
        named = False
        async for sse in source.aiter_sse():
            count += 1
            if count > MAX_EVENTS or len(sse.data) > MAX_EVENT_BYTES:
                raise ProviderError(Failure.PROTOCOL)
            if sse.data.strip() == "[DONE]":
                return
            try:
                chunk = json.loads(sse.data)
            except ValueError:
                raise ProviderError(Failure.PROTOCOL) from None
            if not isinstance(chunk, dict):
                raise ProviderError(Failure.PROTOCOL)
            if "error" in chunk:  # remote error text is dropped on purpose
                raise ProviderError(Failure.TRANSIENT)
            model = chunk.get("model")
            if not named and isinstance(model, str) and model:
                named = True
                yield Started(model[:128])
            choices = chunk.get("choices")
            if isinstance(choices, list) and choices:
                choice = choices[0]
                delta = choice.get("delta") if isinstance(choice, dict) else None
                content = delta.get("content") if isinstance(delta, dict) else None
                if isinstance(content, str) and content:
                    yield Delta(content)
                if isinstance(delta, dict) and "tool_calls" in delta:
                    for event in _tool_events(delta["tool_calls"]):
                        yield event
                reason = choice.get("finish_reason") if isinstance(choice, dict) else None
                if isinstance(reason, str):
                    yield Finished(_REASONS.get(reason, "other"))  # type: ignore[arg-type]
            if "usage" in chunk and chunk["usage"] is not None:
                yield UsageReported(usage_from(chunk["usage"]))
