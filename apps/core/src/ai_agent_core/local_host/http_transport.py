"""Offline non-secret HTTP transport setting. No grants, requests or model changes."""

from pathlib import Path

from .layout import HostConfig, HostError, load_config, paths, save_config
from .process import exclusive_host


def configure_http_transport(root: Path, *, keepalive_seconds: int) -> None:
    host = paths(root)
    with exclusive_host(root):
        config = load_config(host)
        if config.live is None:
            raise HostError("LIVE_NOT_PREPARED")
        data = config.model_dump(mode="json")
        data["live"]["http_keepalive_seconds"] = keepalive_seconds
        try:
            updated = HostConfig.model_validate(data)
        except ValueError:
            raise HostError("HTTP_KEEPALIVE_INVALID") from None
        save_config(host, updated)
