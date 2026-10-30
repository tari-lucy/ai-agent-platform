"""Local fake SOCKS5 and Telegram: no tokens, API calls or public publications."""

import json
import logging
import socket
import threading
from contextlib import contextmanager

import httpcore
import pytest
from ai_agent_adapters.telegram.bot import TelegramBot, TelegramError
from ai_agent_adapters.telegram.proxy import checked_proxy
from pydantic import SecretStr


@pytest.mark.parametrize(
    "proxy",
    [
        "http://127.0.0.1:1080",
        "socks5://example.com:1080",
        "socks5://10.0.0.1:1080",
        "socks5://127.0.0.1",
        "socks5://127.0.0.1:0",
        "socks5://127.0.0.1:99999",
        "socks5://user:password@127.0.0.1:1080",
        "socks5://127.0.0.1:1080/path",
        "socks5://127.0.0.1:1080?x=1",
        "socks5://127.0.0.1:1080#x",
        "socks5://127.0.0.1:1080\n",
    ],
)
def test_proxy_is_explicit_local_socks_only(proxy):
    with pytest.raises(TelegramError, match="TELEGRAM_PROXY_INVALID"):
        checked_proxy(proxy)


@contextmanager
def fake_socks(status=200, payload=None):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    seen = {}
    errors = []

    def worker():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(5)
                with connection.makefile("rb") as reader:
                    version, count = reader.read(2)
                    assert version == 5 and reader.read(count) == b"\x00"
                    connection.sendall(b"\x05\x00")
                    assert reader.read(4) == b"\x05\x01\x00\x03"
                    size = reader.read(1)[0]
                    seen["destination"] = reader.read(size).decode()
                    seen["port"] = int.from_bytes(reader.read(2), "big")
                    connection.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
                    seen["request"] = reader.readline().decode()
                    headers = {}
                    while (line := reader.readline()) != b"\r\n":
                        key, value = line.decode().split(":", 1)
                        headers[key.lower()] = value.strip()
                    seen["body"] = reader.read(int(headers["content-length"]))
                    data = payload or b'{"ok":true,"result":{"message_id":19}}'
                    connection.sendall(
                        f"HTTP/1.1 {status} Test\r\nContent-Length: {len(data)}\r\n"
                        "Connection: close\r\n\r\n".encode()
                        + data
                    )
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        yield f"socks5://127.0.0.1:{listener.getsockname()[1]}", seen
    finally:
        listener.close()
        thread.join(timeout=6)
        assert not thread.is_alive()
        assert not errors


def test_post_goes_through_socks_with_remote_dns_and_no_url_logging(caplog):
    caplog.set_level(logging.DEBUG)
    with fake_socks() as (proxy, seen):
        bot = TelegramBot(
            SecretStr("123:fictional-secret"), api="http://bot-api.invalid", proxy=proxy
        )
        assert bot.send_message("@fictional", "synthetic") == 19
    assert seen["destination"] == "bot-api.invalid" and seen["port"] == 80
    assert json.loads(seen["body"]) == {"chat_id": "@fictional", "text": "synthetic"}
    assert "fictional-secret" not in caplog.text


def test_proxy_http_refusal_is_fixed_and_not_retried():
    with fake_socks(status=403) as (proxy, seen):
        bot = TelegramBot(
            SecretStr("123:fictional-secret"), api="http://bot-api.invalid", proxy=proxy
        )
        with pytest.raises(TelegramError) as error:
            bot.send_message("@fictional", "synthetic")
    assert error.value.status == 403
    assert "fictional-secret" not in repr(error.value)
    assert seen["request"].startswith("POST ")


@pytest.mark.parametrize("payload", [b"invalid-json", b'{"ok":true,"result":{}}'])
def test_unusable_success_reply_is_unknown_not_safe_to_repeat(payload):
    with fake_socks(payload=payload) as (proxy, _):
        bot = TelegramBot(
            SecretStr("123:fictional-secret"), api="http://bot-api.invalid", proxy=proxy
        )
        with pytest.raises(TelegramError, match="TELEGRAM_OUTCOME_UNKNOWN"):
            bot.send_message("@fictional", "synthetic")


@pytest.mark.parametrize(
    "failure,code",
    [
        (httpcore.ConnectError("fictional-secret"), "TELEGRAM_UNAVAILABLE"),
        (httpcore.ReadTimeout("fictional-secret"), "TELEGRAM_OUTCOME_UNKNOWN"),
    ],
)
def test_failed_proxy_does_not_fall_back_or_leak(monkeypatch, failure, code):
    calls = []

    class Pool:
        def __init__(self, **kwargs):
            assert kwargs["retries"] == 0
            assert kwargs["ssl_context"].check_hostname is True

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def stream(self, *args, **kwargs):
            calls.append(1)
            raise failure

    def direct(*args, **kwargs):
        pytest.fail("Configured SOCKS proxy must not fall back to direct urllib")

    monkeypatch.setattr(httpcore, "ConnectionPool", Pool)
    monkeypatch.setattr("urllib.request.urlopen", direct)
    bot = TelegramBot(SecretStr("123:fictional-secret"), proxy="socks5://127.0.0.1:10880")
    with pytest.raises(TelegramError) as error:
        bot.send_message("@fictional", "synthetic")
    assert error.value.code == code
    assert "fictional-secret" not in repr(error.value)
    assert calls == [1]
