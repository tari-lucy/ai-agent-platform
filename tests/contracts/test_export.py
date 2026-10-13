"""Generated contracts are stable, internally consistent and valid OpenAPI 3.1."""

import json
from pathlib import Path

from ai_agent_contracts.catalogue import ADAPTERS, READ_MODELS
from ai_agent_contracts.export import artifacts, json_schema, openapi
from fastapi.openapi.models import OpenAPI

ROOT = Path(__file__).resolve().parents[2]


def walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def resolve(root, pointer):
    value = root
    assert pointer.startswith("#/")
    for name in pointer[2:].split("/"):
        value = value[name.replace("~1", "/").replace("~0", "~")]
    return value


def test_openapi_model_references_ids_and_auth_boundaries():
    document = openapi()
    validated = OpenAPI.model_validate(document)
    assert validated.openapi == "3.1.0"
    ids = []
    for path, methods in document["paths"].items():
        for method, operation in methods.items():
            ids.append(operation["operationId"])
            security = operation["security"]
            if path.startswith("/internal"):
                assert security == [{"ServiceIdentity": []}]
            elif path == "/api/v1/auth/login":
                assert security == []
                assert operation["x-origin-check"] is True
            elif method == "post":
                assert security == [{"OperatorSession": [], "CSRF": []}]
            else:
                assert security == [{"OperatorSession": []}]
            assert "ApiError" in json.dumps(operation["responses"])
    assert len(ids) == len(set(ids))
    for node in walk(document):
        if "$ref" in node:
            assert resolve(document, node["$ref"]) is not None
    assert document["x-runtime-implemented"] is False
    assert document["x-websocket"]["origin_check"] is True
    assert document["x-websocket"]["scope_check"] is True


def test_jsonschema_references_and_closed_models():
    document = json_schema()
    for node in walk(document):
        if "$ref" in node:
            assert resolve(document, node["$ref"]) is not None
    for name, adapter in ADAPTERS.items():
        schema = adapter.json_schema()
        # Each concrete object has a closed field set.
        for node in walk(schema):
            if "properties" in node:
                assert node.get("additionalProperties") is False, name
    assert {
        "personas",
        "sources",
        "people",
        "relationships",
        "topics",
        "promises",
        "arcs",
        "changes",
        "operations",
        "llm-attempts",
        "tariffs",
        "laboratory-reports",
    }.issubset(READ_MODELS)


def test_committed_exports_are_deterministic():
    first, second = artifacts(), artifacts()
    assert first == second
    for name, content in first.items():
        assert (ROOT / "packages/contracts/generated" / name).read_text(encoding="utf-8") == content


def test_independent_api_schema_and_record_versions():
    doc = openapi()
    assert doc["info"]["version"] == "1.0"
    schema = doc["components"]["schemas"]["EditCommand"]
    assert schema["properties"]["schema_version"]["const"] == "1.0"
    assert schema["properties"]["expected_version"]["minimum"] == 1
    assert schema["additionalProperties"] is False
    assert "expected_version" not in doc["components"]["schemas"]["StopCommand"]["properties"]
    assert doc["x-limits"]["body_bytes"] == 1048576
