"""Which model the persona runs on: list what the provider offers, test one, switch to it.

``list_models`` and ``test_model`` use the host's own explicit .env binding (never printed);
``set_model`` is offline like the other host settings (the server must be stopped). Only model
ids, status codes and short provider messages are printed: no key, no endpoint, no content.
"""

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from .layout import HostError, LiveConfig, load_config, paths, save_config
from .process import exclusive_host
from .provider import provider_factory

USER_AGENT = "ai-agent-platform/1.0"  # some gateways refuse the default Python agent


def _binding(root: Path, env_file: Path) -> tuple[str, str, str]:
    """The provider's base URL and key for this host, and the model it is set to now."""
    config = load_config(paths(root))
    if config.live is None:
        raise HostError("LIVE_NOT_PREPARED")
    providers = provider_factory("live", live=config.live, env_file=env_file)
    if providers.binding is None:
        raise HostError("LLM_BINDING_REQUIRED")
    return providers.binding[0], providers.binding[1], config.live.api_model


def _call(url: str, key: str, body: dict[str, object] | None, timeout: float) -> tuple[int, object]:
    request = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        method="GET" if body is None else "POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read(4 * 1024 * 1024) or b"null")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read(65536) or b"null")
        except ValueError:
            return error.code, None
    except OSError, ValueError:
        return 0, None


def list_models(root: Path, env_file: Path) -> tuple[str, list[str]]:
    """(current model id, every id the provider lists), sorted."""
    base, key, current = _binding(root, env_file)
    status, data = _call(base.rstrip("/") + "/models", key, None, 30)
    rows = data.get("data") if isinstance(data, dict) else None
    if status != 200 or not isinstance(rows, list):
        raise HostError(f"MODEL_LIST_UNAVAILABLE_{status}")
    ids = sorted(
        {row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
    )
    return current, ids


def test_model(root: Path, env_file: Path, model: str) -> tuple[bool, str]:
    """One tiny real request (about 8 output tokens): does this model answer, and how fast."""
    base, key, _ = _binding(root, env_file)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Ответь одним словом: да"}],
        "max_tokens": 8,
    }
    started = time.monotonic()
    status, data = _call(base.rstrip("/") + "/chat/completions", key, body, 90)
    seconds = time.monotonic() - started
    if status == 200 and isinstance(data, dict) and data.get("choices"):
        return True, f"отвечает за {seconds:.1f} с"
    message = ""
    if isinstance(data, dict):
        error = data.get("error")
        message = str(error.get("message") if isinstance(error, dict) else error or "")[:200]
    return False, f"не отвечает (HTTP {status or 'нет связи'}{': ' + message if message else ''})"


def set_model(root: Path, model: str) -> str:
    """Offline: the persona's model id; the service must be stopped. Returns the previous id."""
    host = paths(root)
    with exclusive_host(host.root):
        config = load_config(host)
        if config.live is None:
            raise HostError("LIVE_NOT_PREPARED")
        previous = config.live.api_model
        # A name with a slash is not a contract-safe id: keep an internal alias, ask the provider
        # for the real name.
        alias = re.sub(r"[^A-Za-z0-9_.:-]+", "-", model).strip("-.:_")
        values = {
            **config.live.model_dump(),
            "model_id": alias or "model",
            "provider_model": None if alias == model else model,
        }
        try:
            live = LiveConfig.model_validate(values)
        except ValueError:
            raise HostError("INVALID_OPTION") from None
        save_config(host, config.model_copy(update={"live": live}))
    return previous
