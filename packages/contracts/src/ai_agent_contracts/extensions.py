"""Local schema registrations, supplied by trusted owners rather than input messages."""

import json
from typing import Any

from pydantic import BaseModel, TypeAdapter

from ai_agent_contracts.common import Contract, Id


class ExtensionRegistry:
    def __init__(self) -> None:
        self._schemas: dict[tuple[str, str], type[Contract]] = {}

    def register(self, schema_id: str, model: type[Contract], version: str = "1.0") -> None:
        TypeAdapter(Id).validate_python(schema_id)
        if version != "1.0" or (schema_id, version) in self._schemas:
            raise ValueError("REGISTRATION_CONFLICT")
        if not issubclass(model, Contract) or model.model_config.get("extra") != "forbid":
            raise ValueError("CLOSED_SCHEMA_REQUIRED")
        self._schemas[schema_id, version] = model

    def validate(self, schema_id: str, version: str, data: Any) -> BaseModel:
        model = self._schemas.get((schema_id, version))
        if model is None:
            raise ValueError("UNKNOWN_EXTENSION_SCHEMA")
        return model.model_validate_json(
            json.dumps(data, ensure_ascii=False, allow_nan=False),
            context={"extensions": self},
        )

    def schemas(self) -> dict[str, dict[str, Any]]:
        return {
            schema_id + "@" + version: model.model_json_schema()
            for (schema_id, version), model in sorted(self._schemas.items())
        }
