"""Is this copy compatible with the host it would replace? Read-only; fixed codes, no values.

A copy holds one database, but a host is more than that: host.json names the scopes and the live
pilot, the database holds grants, role assignments and the durable API attempt counter. Restoring
a copy must not bring back another host's roles, wider rights or a lower API attempt count.
"""

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from ai_agent_application.backup import Issue, Manifest
from ai_agent_application.backup.model import DATABASE
from pydantic import ValidationError

from ai_agent_core.local_host.init import test_scope, work_scope
from ai_agent_core.local_host.layout import MAX_CONFIG_BYTES, HostConfig, HostPaths

from .layout import ALLOWED_EXTRA, HOST_CONFIG

# Fields that decide what a live role or a right can do; volatile fields (verification, state)
# are deliberately not compared.
BINDING_FIELDS = {
    "Connection": ("adapter_id", "endpoint_setting", "credential_setting"),
    "LLMAssignment": ("role_id", "connection_id", "model_id"),
    "Capability": ("adapter_id", "effect", "required_permissions", "resource_ids"),
    "Permission": (
        "principal_id",
        "capability_id",
        "operation_type",
        "binding_id",
        "level",
        "expires_at",
    ),
}
IDENTITY = (
    "schema_version",
    "workspace_id",
    "persona_id",
    "work_space_id",
    "test_space_ids",
    "operator_controls",
    "settings_space_id",
)
GRANT_COLUMNS = (
    "scope_id, principal_id, owner, role, actor_owner, read, write, api_transfer,"
    " public_disclosure, expires_at"
)


def _open(path: Path) -> sqlite3.Connection:
    # Without a -wal file there is nothing to recover, and an immutable read leaves no -wal/-shm
    # behind the live database; with one, a normal read-only open applies it.
    wal = Path(str(path) + "-wal")
    mode = "mode=ro&immutable=1" if not wal.exists() else "mode=ro"
    return sqlite3.connect(f"{path.as_uri()}?{mode}", uri=True)


def _bindings(connection: sqlite3.Connection) -> dict[tuple[str, str, str], dict[str, Any]]:
    rows = connection.execute(
        "SELECT r.scope_id, r.kind, r.id, v.payload FROM records r JOIN record_revisions v"
        " ON r.scope_id=v.scope_id AND r.kind=v.kind AND r.id=v.id AND r.version=v.version"
        f" WHERE r.kind IN ({','.join('?' * len(BINDING_FIELDS))})",
        tuple(BINDING_FIELDS),
    ).fetchall()
    found: dict[tuple[str, str, str], dict[str, Any]] = {}
    for scope_id, kind, identifier, payload in rows:
        data = json.loads(payload)
        found[(scope_id, kind, identifier)] = {f: data.get(f) for f in BINDING_FIELDS[kind]}
    return found


def _summary(path: Path) -> dict[str, Any]:
    connection = _open(path)
    try:
        scopes = connection.execute(
            "SELECT workspace_id, persona_id, memory_space_id, memory_mode FROM scopes"
        ).fetchall()
        grants = connection.execute(f"SELECT {GRANT_COLUMNS} FROM storage_grants").fetchall()
        attempts = connection.execute(
            "SELECT COUNT(*) FROM records WHERE kind='LLMAttempt'"
        ).fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        return {
            "scopes": sorted(tuple(r) for r in scopes),
            "grants": sorted(tuple(map(str, r)) for r in grants),
            "bindings": _bindings(connection),
            "attempts": int(attempts),
            "retention": "retention_usage_aggregates" in tables,
        }
    finally:
        connection.close()


def _read_config(path: Path) -> HostConfig | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            return None
        return HostConfig.model_validate(json.loads(raw))
    except OSError, ValueError, ValidationError:
        return None


def _expected_scopes(config: HostConfig) -> list[tuple[str, str, str, str]]:
    work = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
    spaces = [*config.test_space_ids]
    if config.settings_space_id is not None:
        spaces.append(config.settings_space_id)
    every = (work, *(test_scope(work, space) for space in spaces))
    return sorted((s.workspace_id, s.persona_id, s.memory_space_id, s.memory_mode) for s in every)


def _assignments_fit(config: HostConfig, bindings: dict[tuple[str, str, str], Any]) -> bool:
    """Every role is on the technical mock, except `character` on the prepared live pilot."""
    seen = 0
    for (_, kind, identifier), fields in bindings.items():
        if kind != "LLMAssignment":
            continue
        seen += 1
        live = config.live is not None and fields["role_id"] == "character"
        wanted = (
            ("conn-live", config.live.model_id) if live and config.live else ("conn-mock", None)
        )
        if fields["connection_id"] != wanted[0]:
            return False
        if wanted[1] is not None and fields["model_id"] != wanted[1]:
            return False
        if identifier != "asg-" + str(fields["role_id"]):
            return False
    return seen > 0


class HostCompatibility:
    """The `restore_check` of the local backup service."""

    def __init__(self, host: HostPaths, config: HostConfig) -> None:
        self.host, self.config = host, config

    async def __call__(self, manifest: Manifest, copy_dir: Path) -> list[Issue]:
        return await asyncio.to_thread(self._check, manifest, copy_dir)

    def _check(self, manifest: Manifest, copy_dir: Path) -> list[Issue]:
        names = {f.name for f in manifest.files}
        issues: list[Issue] = []
        if not names <= ALLOWED_EXTRA | {DATABASE}:
            issues.append(Issue("FILE_NOT_ALLOWED"))
        copied = _read_config(copy_dir / HOST_CONFIG) if HOST_CONFIG in names else None
        if copied is None:
            issues.append(Issue("HOST_CONFIG_MISSING"))
        elif any(getattr(copied, f) != getattr(self.config, f) for f in IDENTITY) or (
            (copied.live is None) != (self.config.live is None)
            or (
                copied.live is not None
                and self.config.live is not None
                and copied.live.model_id != self.config.live.model_id
            )
        ):
            issues.append(Issue("HOST_CONFIG_INCOMPATIBLE"))
        try:
            theirs = _summary(copy_dir / DATABASE)
            ours = _summary(self.host.database)
        except sqlite3.Error, OSError, ValueError, KeyError:
            issues.append(Issue("HOST_STATE_UNREADABLE"))
            return issues
        if theirs["scopes"] != _expected_scopes(self.config):
            issues.append(Issue("SCOPES_INCOMPATIBLE"))
        if theirs["grants"] != ours["grants"]:
            issues.append(Issue("GRANTS_DIFFER"))
        if theirs["bindings"] != ours["bindings"] or not _assignments_fit(
            self.config, theirs["bindings"]
        ):
            issues.append(Issue("ROLE_BINDINGS_DIFFER"))
        if self.config.live is not None and theirs["attempts"] < ours["attempts"]:
            issues.append(Issue("API_ATTEMPTS_WOULD_ROLL_BACK"))  # the cap must not be reset
        if not theirs["retention"]:
            issues.append(Issue("RETENTION_NOT_INSTALLED"))
        return issues
