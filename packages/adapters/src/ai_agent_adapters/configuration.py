"""Validated bootstrap configuration, not agent state, access or public API contracts."""

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

MAX_CONFIG_BYTES = 256 * 1024
IDENTIFIER = r"^[A-Za-z0-9_-]{1,64}$"


class ConfigurationError(Exception):
    """Only fixed labels/codes; never raw input, paths, values or chained exceptions."""

    def __init__(self, field: str, code: str) -> None:
        self.field = field
        self.code = code
        super().__init__(f"Configuration rejected: {field}: {code}")


class WorkspaceProfile(BaseModel):
    """Nonsecret local draft settings; accepted persona canon belongs to M2."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    schema_version: Literal["1.0"] = "1.0"
    workspace_id: str = Field(default="default", pattern=IDENTIFIER)
    persona_id: str = Field(default="default", pattern=IDENTIFIER)
    memory_scope: Literal["work", "test"] = "work"
    core_host_port: int = Field(default=8765, ge=1024, le=65535)
    core_container_port: int = Field(default=8080, ge=1024, le=65535)
    core_memory_mib: int = Field(default=768, ge=128, le=768)
    worker_memory_mib: int = Field(default=1536, ge=512, le=1536)
    redis_memory_mib: int = Field(default=256, ge=128, le=256)
    embedding_threads: int = Field(default=2, ge=1, le=2)
    embedding_batch: int = Field(default=4, ge=1, le=4)
    pending_jobs: int = Field(default=256, ge=1, le=256)
    recall_fragments: int = Field(default=8, ge=1, le=8)
    recall_tokens: int = Field(default=2000, ge=128, le=2000)
    autostart: Literal[False] = False

    @field_validator("autostart", mode="before")
    @classmethod
    def parse_disabled(cls, value: Any) -> Any:
        if isinstance(value, str):
            if value.lower() in ("false", "0"):
                return False
            if value.lower() in ("true", "1"):
                return True
        return value


class BootstrapSettings(WorkspaceProfile, BaseSettings):
    """Credentials repr/JSON are redacted, and summaries use an explicit allowlist."""

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        env_prefix="AI_PLATFORM_",
        env_file=None,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # The loader already merged its explicit allowlist; no implicit environment/files.
        return (init_settings,)

    redis_password: SecretStr | None = None
    llm_endpoint: SecretStr | None = None
    llm_api_key: SecretStr | None = None

    @field_validator("redis_password", "llm_endpoint", "llm_api_key", mode="before")
    @classmethod
    def blank_secret(cls, value: Any) -> Any:
        return None if value == "" else value


@dataclass(frozen=True)
class StartupReceipt:
    """Fresh stopped bootstrap receipt; M1 owns the actual supervisor state machine."""

    epoch: str
    state: Literal["STOPPED"] = "STOPPED"
    effects_enabled: Literal[False] = False
    previous_state_ignored: bool = False

    def safe_manifest(self, settings: BootstrapSettings) -> dict[str, str | int | bool]:
        return {
            "configuration_version": settings.schema_version,
            "bootstrap_state": self.state,
            "effects_enabled": self.effects_enabled,
            "epoch": self.epoch,
            "previous_state_ignored": self.previous_state_ignored,
            "core_container_port": settings.core_container_port,
            "core_memory_mib": settings.core_memory_mib,
            "worker_memory_mib": settings.worker_memory_mib,
            "embedding_threads": settings.embedding_threads,
            "embedding_batch": settings.embedding_batch,
            "llm_bound": settings.llm_endpoint is not None and settings.llm_api_key is not None,
        }


def fresh_startup(previous_state: str | None = None) -> StartupReceipt:
    """Restart/restore/sleep do not grant capabilities or reuse a persisted epoch."""
    return StartupReceipt(epoch=uuid4().hex, previous_state_ignored=previous_state is not None)


def validation_error(error: ValidationError, allowed: set[str]) -> ConfigurationError:
    # Even error loc and ctx can contain malicious input. Only known root fields are retained.
    first = error.errors(include_input=False, include_context=False, include_url=False)[0]
    location = first["loc"][0] if first["loc"] else None
    field = location if isinstance(location, str) and location in allowed else "profile"
    types = {
        "literal_error": "unsupported_value",
        "extra_forbidden": "unknown_field",
        "int_parsing": "integer_required",
        "int_type": "integer_required",
        "greater_than_equal": "below_minimum",
        "less_than_equal": "above_maximum",
        "string_pattern_mismatch": "invalid_identifier",
        "string_type": "string_required",
    }
    code = types.get(first["type"], "invalid_value")
    if field == "autostart":
        code = "must_be_disabled"
    return ConfigurationError(field, code)


def read_profile(path: Path, *, partial: bool = False) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        raise ConfigurationError("profile", "unreadable_file") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigurationError("profile", "too_large")
    try:
        data = json.loads(raw)
    except UnicodeDecodeError, json.JSONDecodeError, RecursionError:
        raise ConfigurationError("profile", "invalid_json") from None
    if not isinstance(data, dict):
        raise ConfigurationError("profile", "object_required")
    if not partial and "schema_version" not in data:
        raise ConfigurationError("schema_version", "required")
    try:
        WorkspaceProfile.model_validate(data)
    except ValidationError as error:
        raise validation_error(error, set(WorkspaceProfile.model_fields)) from None
    return data


def validate_bindings(settings: BootstrapSettings) -> None:
    if settings.redis_password is not None:
        value = settings.redis_password.get_secret_value()
        if re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
            raise ConfigurationError("redis_password", "64_hex_characters_required")
    if (settings.llm_endpoint is None) != (settings.llm_api_key is None):
        raise ConfigurationError("llm_binding", "endpoint_and_key_required_together")
    if settings.llm_endpoint is not None:
        try:
            parsed = urlsplit(settings.llm_endpoint.get_secret_value())
            valid = (
                parsed.scheme in ("http", "https")
                and parsed.hostname is not None
                and parsed.username is None
                and parsed.password is None
                and not parsed.fragment
            )
            _ = parsed.port  # reject malformed/overflowing ports without exposing the URL
        except ValueError:
            valid = False
        if not valid:
            raise ConfigurationError("llm_endpoint", "invalid_http_endpoint")


def load_configuration(
    *,
    profile_path: Path | None = None,
    overrides_path: Path | None = None,
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> BootstrapSettings:
    """Defaults -> selected profile -> local overrides -> explicit binding/process env.

    Never discovers/reads a .env implicitly. Runtime may consume an explicitly
    selected local binding; the coding agent only tests synthetic fixture values.
    """
    payload: dict[str, Any] = {}
    if profile_path is not None:
        payload.update(read_profile(profile_path))
    if overrides_path is not None:
        payload.update(read_profile(overrides_path, partial=True))
    process = dict(os.environ if environ is None else environ)
    env_values: dict[str, str | None] = {}
    if env_file is not None:
        try:
            if env_file.stat().st_size > MAX_CONFIG_BYTES:
                raise ConfigurationError("env_file", "too_large")
            env_values.update(dotenv_values(env_file, interpolate=False, encoding="utf-8"))
        except OSError, UnicodeDecodeError:
            raise ConfigurationError("env_file", "unreadable_file") from None
    env_values.update(process)
    for field in BootstrapSettings.model_fields:
        key = "AI_PLATFORM_" + field.upper()
        if key in env_values:
            payload[field] = env_values[key]
    # M0-04 transport alias is process-only and higher precedence than profile files.
    if "CORE_CONTAINER_PORT" in process:
        payload["core_container_port"] = process["CORE_CONTAINER_PORT"]
    # M0-04 Compose passes this binding only to core/Redis.
    if "REDIS_PASSWORD" in process:
        if "redis_password" in payload and payload["redis_password"] != process["REDIS_PASSWORD"]:
            raise ConfigurationError("redis_password", "conflicting_bindings")
        payload["redis_password"] = process["REDIS_PASSWORD"]
    try:
        # Explicit environ argument must be deterministic, independent of host's environment.
        # Bypass BaseSettings auto env source; all allowlisted sources were merged above.
        settings = BootstrapSettings.model_validate(payload)
    except ValidationError as error:
        raise validation_error(error, set(BootstrapSettings.model_fields)) from None
    validate_bindings(settings)
    return settings


def from_environment() -> BootstrapSettings:
    env = os.environ

    def optional_path(name: str) -> Path | None:
        value = env.get(name)
        return Path(value) if value else None

    return load_configuration(
        profile_path=optional_path("AI_PLATFORM_WORKSPACE_PROFILE"),
        overrides_path=optional_path("AI_PLATFORM_LOCAL_OVERRIDES"),
        env_file=optional_path("AI_PLATFORM_ENV_FILE"),
    )
