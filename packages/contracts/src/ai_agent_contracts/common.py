"""Shared wire primitives; no transport, database, adapter or model identity."""

from datetime import UTC as UTC_ZONE
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    ValidationInfo,
    model_validator,
)

SCHEMA_VERSION: Literal["1.0"] = "1.0"
MAX_BODY_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 16000
MAX_LIST_ITEMS = 200
MAX_EXTENSION_BYTES = 65536

Id = Annotated[
    str, Field(strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
]
Version = Annotated[int, Field(strict=True, ge=1)]
Counter = Annotated[int, Field(strict=True, ge=0)]
Text = Annotated[str, Field(strict=True, max_length=MAX_TEXT_CHARS)]
ShortText = Annotated[str, Field(strict=True, max_length=512)]
Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Money = Annotated[str, Field(strict=True, max_length=64, pattern=r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")]


def utc(value: datetime) -> datetime:
    offset = value.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("UTC_REQUIRED")
    return value.astimezone(UTC_ZONE)


UTC = Annotated[datetime, Field(strict=True), AfterValidator(utc)]


def true_flag(value: object) -> bool:
    if value is not True:
        raise ValueError("TRUE_FLAG_REQUIRED")
    return True


TrueFlag = Annotated[Literal[True], BeforeValidator(true_flag)]


class Contract(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        validate_default=True,
        hide_input_in_errors=True,
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def validate_link_scopes(self) -> Self:
        scope = getattr(self, "scope", None)
        if not isinstance(scope, Scope):
            return self
        if str(getattr(self, "provenance", "")) == "simulation" and scope.memory_mode != "test":
            raise ValueError("SIMULATION_SCOPE_REQUIRED")

        def visit(value: object) -> None:
            if isinstance(value, BaseModel):
                other = getattr(value, "scope", None)
                if isinstance(other, Scope) and other.partition != scope.partition:
                    raise ValueError("SCOPE_MISMATCH")
                for name in type(value).model_fields:
                    visit(getattr(value, name))
            elif isinstance(value, (list, tuple)):
                for item in value:
                    visit(item)
            elif isinstance(value, dict):
                for item in value.values():
                    visit(item)

        for name in type(self).model_fields:
            if name != "scope":
                visit(getattr(self, name))
        return self


class Scope(Contract):
    workspace_id: Id
    persona_id: Id
    memory_space_id: Id
    memory_mode: Literal["work", "test"]
    agent_id: Id | None = None
    run_id: Id | None = None
    session_id: Id | None = None

    @property
    def partition(self) -> tuple[str, str, str, str]:
        return self.workspace_id, self.persona_id, self.memory_space_id, self.memory_mode

    @model_validator(mode="after")
    def run_requires_agent(self) -> Self:
        if self.run_id is not None and self.agent_id is None:
            raise ValueError("AGENT_REQUIRED")
        if self.session_id is not None and self.run_id is None:
            raise ValueError("RUN_REQUIRED")
        return self


class Record(Contract):
    id: Id
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    scope: Scope
    created_at: UTC


class VersionedRecord(Record):
    version: Version
    updated_at: UTC

    @model_validator(mode="after")
    def timestamps_ordered(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("TIME_ORDER")
        return self


class SourceRef(Contract):
    source_id: Id
    version: Version
    scope: Scope
    availability: Literal["available", "expired", "deleted", "unavailable"]


class ArtifactRef(Contract):
    artifact_id: Id
    version: Version
    scope: Scope
    checksum: Digest
    size_bytes: Counter
    media_type: ShortText
    # Opaque catalogue ID; never an arbitrary file path, endpoint or signed URL.


class ExtensionPayload(Contract):
    schema_id: Id
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    data: JsonValue

    @model_validator(mode="after")
    def registered(self, info: ValidationInfo) -> Self:
        import json

        encoded = json.dumps(self.data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_EXTENSION_BYTES:
            raise ValueError("EXTENSION_TOO_LARGE")
        from ai_agent_contracts.extensions import ExtensionRegistry

        registry = (info.context or {}).get("extensions")
        if not isinstance(registry, ExtensionRegistry):
            raise ValueError("EXTENSION_REGISTRY_REQUIRED")
        registry.validate(self.schema_id, self.schema_version, self.data)
        return self
