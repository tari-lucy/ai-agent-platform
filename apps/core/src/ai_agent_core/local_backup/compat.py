"""Is this copy compatible with the host it would replace? Read-only; fixed codes, no values.

A copy holds one database, but a host is more than that: host.json names the scopes and the live
pilot, the database holds grants, role assignments and the durable API attempt counter. Restoring
a copy must not bring back another host's roles, wider rights or a lower API attempt count.
"""

import asyncio
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ai_agent_adapters.workspace_files.lifecycle import KINDS as WORKPLACE_KINDS
from ai_agent_application.backup import Issue, Manifest
from ai_agent_application.backup.model import DATABASE
from pydantic import ValidationError

from ai_agent_core.local_host.layout import MAX_CONFIG_BYTES, HostConfig, HostPaths, host_scopes

from .layout import ALLOWED_EXTRA, HOST_CONFIG

AssignmentRule = Callable[[HostConfig, dict[tuple[str, str, str], dict[str, Any]]], bool]

# Execution-relevant bindings and rights. Connection verification is volatile;
# a capability's ready/revoked state and a permission's conditions are authoritative.
BINDING_FIELDS = {
    "CapabilityManifest": (
        "capability_id",
        "capability_version",
        "areas",
        "network",
        "runner_required",
        "effect",
        "input_schema_id",
        "output_schema_id",
    ),
    "CapabilityBinding": (
        "capability_id",
        "capability_version",
        "adapter_id",
        "adapter_version",
        "state",
        "verified_at",
    ),
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
        "conditions",
        "limits",
    ),
}
IDENTITY = (
    "schema_version",
    "workspace_id",
    "persona_id",
    "work_space_id",
    "additional_personas",
    "test_space_ids",
    "operator_controls",
    "settings_space_id",
    "workplace_tickets",
    "workplace_files",
    "workplace_permissions",
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
        jobs = connection.execute(
            "SELECT r.scope_id,r.id,json_extract(v.payload,'$.status') FROM records r "
            "JOIN record_revisions v ON r.scope_id=v.scope_id AND r.kind=v.kind "
            "AND r.id=v.id AND r.version=v.version WHERE r.kind='Job'"
        ).fetchall()
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        return {
            "scopes": sorted(tuple(r) for r in scopes),
            "grants": sorted(tuple(map(str, r)) for r in grants),
            "bindings": _bindings(connection),
            "attempts": int(attempts),
            "jobs": {(scope, identifier): status for scope, identifier, status in jobs},
            "retention": "retention_usage_aggregates" in tables,
            # Include hidden/old revisions: disabling a feature does not erase its copies.
            "workplace": connection.execute(
                "SELECT 1 FROM record_revisions WHERE kind IN ("
                + ",".join("?" for _ in WORKPLACE_KINDS)
                + ") LIMIT 1",
                tuple(WORKPLACE_KINDS),
            ).fetchone()
            is not None,
        }
    finally:
        connection.close()


def _read_config(path: Path) -> HostConfig | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            return None
        return HostConfig.model_validate_json(raw)
    except OSError, ValueError, ValidationError:
        return None


def _expected_scopes(config: HostConfig) -> list[tuple[str, str, str, str]]:
    return sorted(
        (s.workspace_id, s.persona_id, s.memory_space_id, s.memory_mode)
        for s in host_scopes(config)
    )


class HostCompatibility:
    """The `restore_check` of the local backup service."""

    def __init__(
        self,
        host: HostPaths,
        config: HostConfig,
        assignments_fit: AssignmentRule | None = None,
        *,
        workplace_ready: bool = False,
    ) -> None:
        # The rule of admissible role assignments belongs to the coordinator; backup keeps no
        # role list of its own and only compares the copy with the current database.
        self.host, self.config, self.assignments_fit = host, config, assignments_fit
        self.workplace_ready = workplace_ready

    async def __call__(self, manifest: Manifest, copy_dir: Path) -> list[Issue]:
        return await asyncio.to_thread(self._check, manifest, copy_dir)

    def _check(self, manifest: Manifest, copy_dir: Path) -> list[Issue]:
        names = {f.name for f in manifest.files}
        issues: list[Issue] = []
        if (self.host.root / "RESTORE-PENDING").exists():
            issues.append(Issue("RESTORE_PENDING"))
        if not names <= ALLOWED_EXTRA | {DATABASE}:
            issues.append(Issue("FILE_NOT_ALLOWED"))
        copied = _read_config(copy_dir / HOST_CONFIG) if HOST_CONFIG in names else None
        if "workplace-blobs.zip" in names and (copied is None or copied.workplace_files is None):
            issues.append(Issue("WORKPLACE_BUNDLE_UNEXPECTED"))
        if not self.workplace_ready and any(
            config is not None and (config.workplace_files is not None or config.workplace_tickets)
            for config in (self.config, copied)
        ):
            issues.append(Issue("WORKPLACE_RESTORE_NOT_READY"))
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
        if (
            not self.workplace_ready
            and (theirs["workplace"] or ours["workplace"])
            and not any(issue.code == "WORKPLACE_RESTORE_NOT_READY" for issue in issues)
        ):
            issues.append(Issue("WORKPLACE_RESTORE_NOT_READY"))
        if theirs["scopes"] != _expected_scopes(self.config):
            issues.append(Issue("SCOPES_INCOMPATIBLE"))
        if theirs["grants"] != ours["grants"]:
            issues.append(Issue("GRANTS_DIFFER"))
        if theirs["bindings"] != ours["bindings"] or (
            self.assignments_fit is not None
            and not self.assignments_fit(self.config, theirs["bindings"])
        ):
            issues.append(Issue("ROLE_BINDINGS_DIFFER"))
        if self.config.live is not None and theirs["attempts"] < ours["attempts"]:
            issues.append(Issue("API_ATTEMPTS_WOULD_ROLL_BACK"))  # the cap must not be reset
        closed = {"SUCCEEDED", "FAILED", "REJECTED", "CANCELLED", "EXPIRED"}
        if any(
            key not in theirs["jobs"] and status not in closed
            for key, status in ours["jobs"].items()
        ):
            issues.append(Issue("LIVE_OPERATIONS_WOULD_BE_LOST"))
        if not theirs["retention"]:
            issues.append(Issue("RETENTION_NOT_INSTALLED"))
        return issues
