"""OpenAI-compatible adapter (M2-04): wire shape, usage, errors, secrecy, real loopback streams."""

import asyncio
import json

import httpx
import pytest
from ai_agent_adapters.llm import OpenAICompatibleProvider, checked_endpoint, usage_from
from ai_agent_application.llm import (
    Delta,
    Failure,
    Finished,
    LLMGateway,
    ProviderCall,
    ProviderError,
    Started,
    UsageReported,
)
from llm_support import (
    CREDENTIAL_SETTING,
    ENDPOINT_SETTING,
    Clock,
    Sleeps,
    request,
    resolved,
    run,
)

KEY = "synthetic-key-0123456789"
BASE = "https://llm.example.invalid/v1"
SETTINGS = {ENDPOINT_SETTING: BASE, CREDENTIAL_SETTING: KEY}


def sse(*chunks, done=True):
    lines = [f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n" for c in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def chunk(content=None, finish=None, model="up-model-1", usage=None, choices=True):
    body = {"id": "c1", "model": model}
    if choices:
        delta = {} if content is None else {"content": content}
        body["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
    else:
        body["choices"] = []
    if usage is not None:
        body["usage"] = usage
    return body


FULL = sse(
    chunk(""),
    chunk("При"),
    chunk("вет"),
    chunk(None, "stop"),
    chunk(
        usage={
            "prompt_tokens": 20,
            "completion_tokens": 6,
            "prompt_tokens_details": {"cached_tokens": 8},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
        choices=False,
    ),
)


def provider_for(handler, settings=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return OpenAICompatibleProvider(SETTINGS if settings is None else settings, client)


def ok(body=FULL):
    return lambda _r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=body
    )


async def collect(provider, assignment=None):
    events = []
    async for event in provider.stream(ProviderCall(assignment or resolved(), request().messages)):
        events.append(event)
    return events


def failure_of(provider, assignment=None):
    with pytest.raises(ProviderError) as info:
        run(collect(provider, assignment))
    return info.value


def test_request_shape_is_chat_completions_stream_without_tools():
    seen = {}

    def handler(r):
        seen["url"] = str(r.url)
        seen["auth"] = r.headers["authorization"]
        seen["body"] = json.loads(r.content)
        return ok()(r)

    assignment = resolved(
        parameters={"max_output_tokens": 77, "timeout_seconds": 9, "temperature": 0.4}
    )
    run(collect(provider_for(handler), assignment))
    assert seen["url"] == BASE + "/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    body = seen["body"]
    assert body["model"] == "model-x" and body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["max_tokens"] == 77 and body["temperature"] == 0.4 and "top_p" not in body
    assert "tools" not in body and "functions" not in body
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_full_stream_is_normalised_with_provider_usage():
    events = run(collect(provider_for(ok())))
    assert events[0] == Started(None) and Started("up-model-1") in events
    assert [e.text for e in events if isinstance(e, Delta)] == ["При", "вет"]
    assert Finished("stop") in events
    usage = [e for e in events if isinstance(e, UsageReported)][-1].usage
    assert (usage.input_tokens, usage.output_tokens) == (20, 6)
    assert (usage.cached_input_tokens, usage.reasoning_output_tokens) == (8, 2)
    assert usage.source == "provider" and usage.completeness == "complete"


def test_usage_without_numbers_or_with_impossible_subsets_is_not_zero():
    assert usage_from(None).source == "unknown"
    assert usage_from({}).input_tokens is None
    assert usage_from({"prompt_tokens": -1, "completion_tokens": True}).source == "unknown"
    partial = usage_from({"prompt_tokens": 7})
    assert partial.completeness == "partial" and partial.output_tokens is None
    odd = usage_from(
        {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 9},
            "completion_tokens_details": {"reasoning_tokens": 4},
        }
    )
    assert odd.cached_input_tokens is None and odd.reasoning_output_tokens is None


def test_stream_without_done_but_with_finish_reason_still_completes_through_the_gateway():
    body = sse(chunk("ок"), chunk(None, "stop"), done=False)
    gw = LLMGateway(lambda _a: provider_for(ok(body)), lambda _r: resolved(), clock=Clock())
    result = run(gw.generate(request()))
    assert result.status == "completed" and result.text == "ок"
    assert result.attempts[0].usage.source == "unknown"


def test_stream_cut_before_a_finish_reason_is_incomplete_through_the_gateway():
    body = sse(chunk("поло"), chunk("вина"), done=False)
    gw = LLMGateway(lambda _a: provider_for(ok(body)), lambda _r: resolved(), clock=Clock())
    result = run(gw.generate(request()))
    assert result.status == "incomplete" and result.text is None and result.preview == "половина"


def test_tool_call_fragments_are_never_forwarded():
    body = sse(
        {
            "model": "m",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"function": {"name": "rm", "arguments": '{"p": "/"'}}]
                    },
                    "finish_reason": None,
                }
            ],
        },
        chunk(None, "tool_calls"),
    )
    events = run(collect(provider_for(ok(body))))
    assert not any(isinstance(e, Delta) for e in events)
    assert Finished("tool_calls") in events
    gw = LLMGateway(lambda _a: provider_for(ok(body)), lambda _r: resolved(), clock=Clock())
    assert run(gw.generate(request())).failure is Failure.UNSUPPORTED


@pytest.mark.parametrize(
    ("status", "headers", "failure"),
    [
        (401, {}, Failure.AUTH),
        (403, {}, Failure.AUTH),
        (429, {"retry-after": "7"}, Failure.RATE_LIMITED),
        (500, {}, Failure.TRANSIENT),
        (503, {}, Failure.TRANSIENT),
        (400, {}, Failure.REJECTED),
        (404, {}, Failure.REJECTED),
        (302, {"location": "https://elsewhere.invalid/"}, Failure.REJECTED),
    ],
)
def test_http_statuses_map_to_neutral_failures(status, headers, failure):
    body = json.dumps({"error": {"message": f"leaky {KEY} text"}}).encode()
    error = failure_of(
        provider_for(lambda _r: httpx.Response(status, headers=headers, content=body))
    )
    assert error.failure is failure
    assert KEY not in str(error) and KEY not in repr(error) and "leaky" not in str(error)
    if status == 429:
        assert error.retry_after == 7.0


def test_a_redirect_is_not_followed_so_the_credential_stays_home():
    hosts = []

    def handler(r):
        hosts.append(r.url.host)
        return httpx.Response(307, headers={"location": "https://elsewhere.invalid/x"})

    failure_of(provider_for(handler))
    assert hosts == ["llm.example.invalid"]


def test_non_stream_success_and_garbage_are_protocol_errors():
    assert (
        failure_of(provider_for(lambda _r: httpx.Response(200, json={"choices": []}))).failure
        is Failure.PROTOCOL
    )
    assert failure_of(provider_for(ok(sse("{not json")))).failure is Failure.PROTOCOL
    assert failure_of(provider_for(ok(sse("[1,2]")))).failure is Failure.PROTOCOL


def test_error_object_in_the_stream_is_transient_and_drops_remote_text():
    body = sse(chunk("a"), {"error": {"message": f"upstream said {KEY}"}}, done=False)
    error = failure_of(provider_for(ok(body)))
    assert error.failure is Failure.TRANSIENT and KEY not in str(error)


def test_oversized_event_and_runaway_stream_are_cut():
    huge = sse({"choices": [{"delta": {"content": "x" * 300_000}}]})
    assert failure_of(provider_for(ok(huge))).failure is Failure.PROTOCOL


def test_transport_failures_are_neutral_and_secret_free():
    def refuse(_r):
        raise httpx.ConnectError(f"cannot connect to {BASE} with {KEY}")

    def slow(_r):
        raise httpx.ReadTimeout(f"timeout {BASE}")

    for handler, failure in ((refuse, Failure.TRANSIENT), (slow, Failure.TIMEOUT)):
        error = failure_of(provider_for(handler))
        assert error.failure is failure
        assert KEY not in str(error) and BASE not in str(error) and error.__cause__ is None


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {ENDPOINT_SETTING: BASE},
        {CREDENTIAL_SETTING: KEY},
        {ENDPOINT_SETTING: "", CREDENTIAL_SETTING: KEY},
        {ENDPOINT_SETTING: BASE, CREDENTIAL_SETTING: KEY + "\nX-Evil: 1"},
    ],
)
def test_missing_or_unsafe_settings_are_not_configured(settings):
    seen = []
    error = failure_of(provider_for(lambda r: seen.append(r) or ok()(r), settings))
    assert error.failure is Failure.NOT_CONFIGURED and seen == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://llm.example.invalid/v1",
        "ftp://llm.example.invalid/v1",
        "https://user:pw@llm.example.invalid/v1",
        "https://user@llm.example.invalid/v1",
        "https://llm.example.invalid/v1?token=1",
        "https://llm.example.invalid/v1#x",
        "https:///v1",
        "http://10.0.0.5/v1",
        "https://llm.example.invalid:99999/v1",
        "not a url",
    ],
)
def test_endpoint_validation_rejects_unsafe_targets(endpoint):
    with pytest.raises(ProviderError) as info:
        checked_endpoint(endpoint)
    assert info.value.failure is Failure.NOT_CONFIGURED


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://llm.example.invalid/v1/",
        "http://127.0.0.1:8080/v1",
        "http://localhost:9/v1",
        "http://[::1]:9/v1",
    ],
)
def test_endpoint_validation_accepts_https_and_loopback(endpoint):
    assert checked_endpoint(endpoint) == endpoint.rstrip("/")


def test_model_endpoint_and_setting_names_come_from_configuration():
    seen = []
    other = {
        "AI_PLATFORM_ALT_URL": "https://alt.example.invalid/api",
        "AI_PLATFORM_ALT_KEY": "k2-synthetic",
    }

    def handler(r):
        seen.append((str(r.url), json.loads(r.content)["model"], r.headers["authorization"]))
        return ok()(r)

    from ai_agent_application.llm import resolve_assignment
    from llm_support import REGISTRY, assignment, connection

    bound = resolve_assignment(
        assignment("asg-z", model="other-model"),
        connection("conn-a", endpoint="AI_PLATFORM_ALT_URL", credential="AI_PLATFORM_ALT_KEY"),
        REGISTRY,
    )
    run(collect(provider_for(handler, other), bound))
    assert seen == [
        ("https://alt.example.invalid/api/chat/completions", "other-model", "Bearer k2-synthetic")
    ]


# -- a real loopback HTTP server: abrupt close and cancellation --------------------------------


class Loopback:
    """Minimal synthetic SSE server on 127.0.0.1 (asyncio), for connection-level behaviour."""

    def __init__(self, script):
        self.script = script
        self.server = None
        self.closed = asyncio.Event()
        self.requests = 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/v1"

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, reader, writer):
        self.requests += 1
        head = await reader.readuntil(b"\r\n\r\n")
        length = int(
            next(
                (
                    line.split(b":")[1]
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length")
                ),
                b"0",
            )
        )
        await reader.readexactly(length)
        writer.write(
            b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
            b"transfer-encoding: chunked\r\n\r\n"
        )
        try:
            await self.script(writer, reader)
        except ConnectionError:
            pass
        finally:
            self.closed.set()
            writer.close()


def send(writer, data: bytes):
    writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")


def loopback_gateway(url):
    provider = OpenAICompatibleProvider({ENDPOINT_SETTING: url, CREDENTIAL_SETTING: KEY})
    return LLMGateway(
        lambda _a: provider,
        lambda _r: resolved(parameters={"max_output_tokens": 32, "timeout_seconds": 5}),
        clock=Clock(),
        sleep=Sleeps(),
        jitter=lambda: 1.0,
    )


def test_connection_dropped_mid_stream_is_incomplete_not_a_result():
    async def script(writer, reader):
        send(writer, sse(chunk("обр"), chunk("ыв"), done=False))
        await writer.drain()
        writer.transport.abort()  # reset without a terminating chunk

    async def scenario():
        async with Loopback(script) as url:
            return await loopback_gateway(url).generate(request())

    result = run(scenario())
    assert result.status == "incomplete" and result.text is None and result.preview == "обрыв"
    assert (
        result.attempts[0].status == "incomplete" and result.attempts[0].usage.source == "unknown"
    )


def test_cancel_closes_the_http_connection_and_drops_the_late_tail():
    async def script(writer, reader):
        send(writer, sse(chunk("раз"), done=False))
        await writer.drain()
        await reader.read()  # returns only when the client hangs up; the tail is never sent

    async def scenario():
        async with Loopback(script) as url:
            cancel = asyncio.Event()
            got = []

            async def on_delta(text):
                got.append(text)
                cancel.set()

            result = await loopback_gateway(url).generate(
                request(), cancel=cancel, on_delta=on_delta
            )
            return result, got

    result, got = run(scenario())
    assert result.status == "cancelled" and result.text is None and got == ["раз"]
    assert result.attempts[0].status == "cancelled"


def test_server_that_closes_after_cancel_is_observed():
    state = {}

    async def script(writer, reader):
        send(writer, sse(chunk("a"), done=False))
        await writer.drain()
        try:
            await asyncio.wait_for(reader.read(), 10)  # ends when the client closes the socket
        finally:
            state["handler_left"] = True

    async def scenario():
        loop = Loopback(script)
        async with loop as url:
            cancel = asyncio.Event()
            task = asyncio.ensure_future(loopback_gateway(url).generate(request(), cancel=cancel))
            await asyncio.sleep(0.2)
            cancel.set()
            result = await task
            await asyncio.wait_for(loop.closed.wait(), 5)  # hang-up reached the server
            return result

    assert run(scenario()).status == "cancelled"
    assert state.get("handler_left")


def test_real_stream_full_roundtrip_over_loopback():
    async def script(writer, reader):
        for part in (chunk("Ну"), chunk(" вот"), chunk(None, "stop")):
            send(writer, sse(part, done=False))
        send(writer, sse(chunk(usage={"prompt_tokens": 5, "completion_tokens": 2}, choices=False)))
        send(writer, b"")
        await writer.drain()

    async def scenario():
        async with Loopback(script) as url:
            return await loopback_gateway(url).generate(request())

    result = run(scenario())
    assert result.status == "completed" and result.text == "Ну вот"
    assert result.attempts[0].usage.output_tokens == 2


def test_the_default_client_does_not_follow_a_redirect_with_the_credential():
    async def scenario():
        other = Loopback(lambda w, r: asyncio.sleep(0))
        async with other as target:

            async def redirect(writer, reader):
                pass

            class Redirecting(Loopback):
                async def handle(self, reader, writer):
                    self.requests += 1
                    await reader.readuntil(b"\r\n\r\n")
                    location = f"{target}/chat/completions"
                    head = f"HTTP/1.1 302 Found\r\nlocation: {location}\r\ncontent-length: 0\r\n"
                    head += "\r\n"
                    writer.write(head.encode())
                    await writer.drain()
                    writer.close()

            async with Redirecting(redirect) as url:
                with pytest.raises(ProviderError) as info:
                    await collect(
                        OpenAICompatibleProvider({ENDPOINT_SETTING: url, CREDENTIAL_SETTING: KEY})
                    )
            return info.value, other.requests

    error, followed = run(scenario())
    assert error.failure is Failure.REJECTED and followed == 0
