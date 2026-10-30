"""Explicit local SSH SOCKS transport; no HTTPX URL logging, redirects or retries.

TLS is verified end to end. The SSH server resolves the Telegram hostname. A failed
configured proxy never falls back to a direct connection. Credentials stay on the host.
"""

import ipaddress
import ssl
from urllib.parse import urlsplit

import httpcore

from .bot import TelegramError

MAX_RESPONSE = 1024 * 1024


def checked_proxy(value: str) -> str:
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme == "socks5"
            and ipaddress.ip_address(parts.hostname or "").is_loopback
            and parts.port is not None
            and parts.port > 0
            and parts.username is None
            and parts.password is None
            and parts.path in ("", "/")
            and not parts.query
            and not parts.fragment
            and not any(c.isspace() for c in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise TelegramError("TELEGRAM_PROXY_INVALID")
    return value


def request_via_proxy(proxy: str, url: str, body: bytes, kind: str, timeout: float) -> bytes:
    timeouts = dict.fromkeys(("connect", "read", "write", "pool"), timeout)
    try:
        with httpcore.ConnectionPool(
            proxy=httpcore.Proxy(checked_proxy(proxy)),
            ssl_context=ssl.create_default_context(),
            max_connections=1,
            retries=0,
        ) as pool:
            with pool.stream(
                "POST",
                url,
                content=body,
                headers={"Content-Type": kind},
                extensions={"timeout": timeouts},
            ) as response:
                if not 200 <= response.status < 300:
                    raise TelegramError("TELEGRAM_REFUSED", response.status)
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_stream():
                    size += len(chunk)
                    if size > MAX_RESPONSE:
                        raise TelegramError("TELEGRAM_OUTCOME_UNKNOWN")
                    chunks.append(chunk)
                return b"".join(chunks)
    except httpcore.ConnectError, httpcore.ConnectTimeout, httpcore.ProxyError:
        raise TelegramError("TELEGRAM_UNAVAILABLE") from None
    except httpcore.NetworkError, httpcore.TimeoutException, httpcore.ProtocolError:
        # After dispatch, never promise that Telegram did not receive the post.
        raise TelegramError("TELEGRAM_OUTCOME_UNKNOWN") from None
    except OSError, ValueError, RuntimeError:
        raise TelegramError("TELEGRAM_UNAVAILABLE") from None
