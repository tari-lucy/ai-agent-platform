"""Explicit offline API cap configuration; no grants, models, credentials or usage are changed."""

import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from .layout import HostConfig, HostError, load_config, paths
from .process import exclusive_host


def configure_budget(root: Path, *, limit: int, period: Literal["lifetime", "utc_day"]) -> None:
    host = paths(root)
    with exclusive_host(root):
        config = load_config(host)
        if config.live is None:
            raise HostError("LIVE_NOT_PREPARED")
        data = config.model_dump(mode="json")
        data["live"]["attempt_limit"] = limit
        data["live"]["attempt_period"] = period
        data["live"]["budget_version"] = config.live.budget_version + 1
        data["live"]["budget_receipt"] = None
        _write(root, data)


def configure_prompt_limit(root: Path, *, max_prompt_chars: int) -> None:
    """The largest live prompt in characters (1000..40000); no grant or usage changes."""
    with exclusive_host(root):
        config = load_config(paths(root))
        if config.live is None:
            raise HostError("LIVE_NOT_PREPARED")
        data = config.model_dump(mode="json")
        data["live"]["max_prompt_chars"] = max_prompt_chars
        _write(root, data)


def _write(root: Path, data: dict[str, Any]) -> None:
    """Validate and atomically replace the host configuration; the caller holds the lock."""
    host = paths(root)
    try:
        updated = HostConfig.model_validate(data)
    except ValueError:
        raise HostError("INVALID_OPTION") from None
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=root,
            prefix=".budget-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(updated.model_dump_json(indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(host.config)
    except OSError:
        raise HostError("CONFIG_WRITE_FAILED") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
