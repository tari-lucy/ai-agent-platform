"""Telegram Bot API, the two calls a channel post needs (doc34 V1).

Created on the host from the explicitly loaded .env, never in the agent container. Errors
carry fixed codes and the HTTP status only: no token, URL, text or remote message.
"""

import json
import secrets
import urllib.error
import urllib.request
from typing import Any

from pydantic import SecretStr

TOKEN_SETTING = "AI_PLATFORM_TELEGRAM_BOT_TOKEN"
PROXY_SETTING = "AI_PLATFORM_TELEGRAM_PROXY"
API = "https://api.telegram.org"
MAX_TEXT = 4096
MAX_CAPTION = 1024


class TelegramError(Exception):
    def __init__(self, code: str, status: int | None = None) -> None:
        self.code, self.status = code, status
        super().__init__(code if status is None else f"{code}:{status}")


class TelegramBot:
    def __init__(
        self, token: SecretStr, *, api: str = API, timeout: float = 60, proxy: str | None = None
    ) -> None:
        self._token, self._api, self._timeout = token, api.rstrip("/"), timeout
        if proxy is not None:
            from .proxy import checked_proxy

            proxy = checked_proxy(proxy)
        self._proxy = proxy

    def send_message(self, chat: str, text: str) -> int:
        if not text.strip() or len(text) > MAX_TEXT:
            raise TelegramError("TELEGRAM_TEXT_INVALID")
        body = json.dumps({"chat_id": chat, "text": text}).encode()
        return self._call("sendMessage", body, "application/json")

    def send_voice(self, chat: str, ogg: bytes, caption: str = "") -> int:
        if len(caption) > MAX_CAPTION:
            raise TelegramError("TELEGRAM_TEXT_INVALID")
        boundary = secrets.token_hex(16)
        fields = [("chat_id", chat)] + ([("caption", caption)] if caption else [])
        head = f"--{boundary}\r\nContent-Disposition: form-data; name="
        parts = [f'{head}"{name}"\r\n\r\n{value}\r\n'.encode() for name, value in fields]
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="voice"; '
            f'filename="voice.ogg"\r\nContent-Type: audio/ogg\r\n\r\n'.encode()
            + ogg
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        return self._call("sendVoice", b"".join(parts), f"multipart/form-data; boundary={boundary}")

    def _call(self, method: str, body: bytes, kind: str) -> int:
        url = f"{self._api}/bot{self._token.get_secret_value()}/{method}"
        request = urllib.request.Request(
            url, data=body, method="POST", headers={"Content-Type": kind}
        )
        try:
            if self._proxy is not None:
                from .proxy import request_via_proxy

                payload = request_via_proxy(self._proxy, url, body, kind, self._timeout)
            else:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    payload = response.read(1024 * 1024)
            reply: Any = json.loads(payload)
        except urllib.error.HTTPError as error:
            raise TelegramError("TELEGRAM_REFUSED", error.code) from None
        except OSError, ValueError:
            if self._proxy is not None:
                raise TelegramError("TELEGRAM_OUTCOME_UNKNOWN") from None
            raise TelegramError("TELEGRAM_UNAVAILABLE") from None
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            if self._proxy is not None and (
                not isinstance(reply, dict) or reply.get("ok") is not False
            ):
                raise TelegramError("TELEGRAM_OUTCOME_UNKNOWN")
            raise TelegramError("TELEGRAM_REFUSED")
        result = reply.get("result")
        identifier = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(identifier, int):
            if self._proxy is not None:
                raise TelegramError("TELEGRAM_OUTCOME_UNKNOWN")
            raise TelegramError("TELEGRAM_REFUSED")
        return identifier
