"""Deterministic offline export. It creates no FastAPI listener or runtime routes."""

import argparse
import json
from pathlib import Path
from typing import Any

from ai_agent_contracts.catalogue import ADAPTERS, READ_MODELS
from ai_agent_contracts.common import MAX_BODY_BYTES, MAX_LIST_ITEMS, MAX_TEXT_CHARS
from ai_agent_contracts.extensions import ExtensionRegistry

Schema = dict[str, Any]


def schema_bundle(ref_template: str = "#/$defs/{model}") -> tuple[dict[str, Schema], Schema]:
    from pydantic import TypeAdapter

    refs, definitions = TypeAdapter.json_schemas(
        [(name, "validation", adapter) for name, adapter in sorted(ADAPTERS.items())],
        ref_template=ref_template,
    )
    return {str(name): schema for (name, _), schema in refs.items()}, definitions["$defs"]


def json_schema() -> Schema:
    roots, definitions = schema_bundle()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "AI Agent Platform contracts",
        "version": "1.0",
        "$defs": definitions,
        "x-contracts": roots,
    }


def openapi(extensions: ExtensionRegistry | None = None) -> Schema:
    refs, definitions = schema_bundle("#/components/schemas/{model}")
    # Named unions may be inline rather than Pydantic definitions.
    for name, schema in refs.items():
        if "$ref" not in schema:
            definitions[name] = schema

    def ref(name: str) -> Schema:
        return refs[name] if "$ref" in refs[name] else {"$ref": "#/components/schemas/" + name}

    operator: list[Schema] = [{"OperatorSession": []}]
    writing: list[Schema] = [{"OperatorSession": [], "CSRF": []}]
    service: list[Schema] = [{"ServiceIdentity": []}]
    scope_parameter_definitions: list[Schema] = [
        {
            "name": name,
            "in": "query",
            "required": True,
            "schema": {
                "type": "string",
                "maxLength": 128,
                "minLength": 1,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
            },
        }
        for name in ("workspace_id", "persona_id", "memory_space_id")
    ] + [
        {
            "name": "memory_mode",
            "in": "query",
            "required": True,
            "schema": {"type": "string", "enum": ["work", "test"]},
        }
    ]

    scope_parameters = [
        {"$ref": "#/components/parameters/" + item["name"]} for item in scope_parameter_definitions
    ]

    def response(name: str, description: str = "Permitted projection") -> Schema:
        return {"description": description, "content": {"application/json": {"schema": ref(name)}}}

    errors = {
        code: {"$ref": "#/components/responses/ApiError"}
        for code in (
            "400",
            "401",
            "403",
            "409",
            "413",
            "422",
            "429",
            "503",
        )
    }
    paths: Schema = {}
    for resource, model in sorted(READ_MODELS.items()):
        paths["/api/v1/" + resource] = {
            "get": {
                "operationId": "list_" + resource.replace("-", "_"),
                "security": operator,
                "parameters": scope_parameters
                + [
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                    },
                    {
                        "name": "cursor",
                        "in": "query",
                        "schema": {"type": "string", "maxLength": 512},
                    },
                ],
                "responses": {"200": response(model.__name__ + "Page"), **errors},
            }
        }
        if resource in {"sources", "utterances", "deliveries", "operations"}:
            paths["/api/v1/" + resource]["get"]["parameters"].append(
                {
                    "name": "order",
                    "in": "query",
                    "schema": {"type": "string", "enum": ["desc"]},
                    "description": "Newest event/creation time then id; opaque time/id cursor.",
                }
            )
        if resource == "deliveries":
            paths["/api/v1/" + resource]["get"]["parameters"].append(
                {
                    "name": "utterance_id",
                    "in": "query",
                    "schema": {"type": "string", "maxLength": 128},
                    "description": "Exact utterance filter; order=desc; current ACL applies.",
                }
            )
        paths["/api/v1/" + resource + "/{resource_id}"] = {
            "get": {
                "operationId": "read_" + resource.replace("-", "_"),
                "security": operator,
                "parameters": scope_parameters
                + [
                    {
                        "name": "resource_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1, "maxLength": 128},
                    },
                ],
                "responses": {
                    "200": response(model.__name__),
                    "404": response("ApiError"),
                    **errors,
                },
            }
        }

    paths["/api/v1/memory/recall"] = {
        "post": {
            "operationId": "panel_memory_recall",
            "security": writing,
            "x-origin-check": True,
            "description": "Local recall: api_transfer, query <=1000, limit <=20; no LLM.",
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": ref("RecallRequest")}},
            },
            "responses": {"200": response("RecallResult"), **errors},
        }
    }

    paths["/api/v1/laboratory-reports/{resource_id}/content"] = {
        "get": {
            "operationId": "download_laboratory_report",
            "security": operator,
            "parameters": scope_parameters
            + [
                {
                    "name": "resource_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
                }
            ],
            "responses": {
                "200": {
                    "description": (
                        "Exact bounded JSON artifact; TEST scope, report/source ACL "
                        "and API rights, TTL, checksum"
                    ),
                    "content": {"application/json": {"schema": {"type": "object"}}},
                    "headers": {
                        "ETag": {
                            "schema": {"type": "string"},
                            "description": "Quoted SHA-256 of stored bytes",
                        },
                        "Content-Disposition": {
                            "schema": {"type": "string"},
                            "description": "Fixed attachment filename",
                        },
                    },
                },
                **errors,
            },
            "x-max-response-bytes": 2_000_000,
            "x-no-store": True,
        }
    }

    memory_download = json.loads(
        json.dumps(paths["/api/v1/laboratory-reports/{resource_id}/content"])
    )
    memory_download["get"]["operationId"] = "download_selected_memory_source"
    memory_download["get"]["responses"]["200"]["description"] = (
        "Selected source JSON after a confirmed export plan; current source ACL/API rights, "
        "version and TTL checked on every download. No server-side export archive."
    )
    memory_download["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
        "$ref": "#/components/schemas/MemorySourceExport"
    }
    memory_download["get"]["responses"]["200"]["headers"]["ETag"]["description"] = (
        "Quoted SHA-256 of response bytes"
    )
    paths["/api/v1/memory/exports/{resource_id}/content"] = memory_download

    def command(path: str, name: str, identity: str, security: list[Schema]) -> None:
        receipt_type = {
            "StopCommand": "StopReceipt",
            "ServiceClaimCommand": "ServiceClaimReceipt",
            "ServiceLeaseCommand": "ServiceLeaseReceipt",
        }.get(name, "Receipt")
        params = []
        for key in ("agent_id", "operation_id"):
            if "{" + key + "}" in path:
                params.append(
                    {
                        "name": key,
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1, "maxLength": 128},
                    }
                )
        paths[path] = {
            "post": {
                "operationId": identity,
                "security": security,
                "parameters": params,
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": ref(name)}},
                },
                "responses": {
                    "200": response(receipt_type, "Known or completed operation"),
                    "202": response(receipt_type, "Accepted is not completed"),
                    **errors,
                },
                "x-owner-checks": ["scope", "current_admission", "idempotency", "versions"],
            }
        }

    command("/api/v1/commands", "PanelCommand", "submit_command", writing)
    command("/api/v1/agents/{agent_id}/stop", "StopCommand", "stop_agent", writing)
    command("/internal/v1/jobs/{operation_id}/claim", "ServiceClaimCommand", "claim_job", service)
    command(
        "/internal/v1/jobs/{operation_id}/lease", "ServiceLeaseCommand", "update_lease", service
    )
    command(
        "/internal/v1/jobs/{operation_id}/result", "ServiceResultCommand", "submit_result", service
    )
    for action, method, security, body in (
        ("login", "post", [], "OperatorLogin"),
        ("session", "get", operator, None),
        ("logout", "post", writing, None),
    ):
        operation: Schema = {
            "operationId": "operator_" + action,
            "security": security,
            "responses": (
                {"204": {"description": "Session revoked"}, **errors}
                if action == "logout"
                else {"200": response("OperatorSession"), **errors}
            ),
            "x-origin-check": True,
        }
        if body:
            operation["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": ref(body)}},
            }
        paths["/api/v1/auth/" + action] = {method: operation}
    paths["/internal/v1/jobs/{operation_id}/lease"]["post"]["responses"]["200"] = {
        "description": "Renewed exact lease or factual cancellation receipt",
        "content": {
            "application/json": {"schema": {"oneOf": [ref("ServiceLeaseReceipt"), ref("Receipt")]}}
        },
    }
    paths["/api/v1/recall"] = {
        "post": {
            "operationId": "recall",
            "security": writing,
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": ref("RecallRequest")}},
            },
            "responses": {"200": response("RecallResult"), **errors},
        }
    }
    for route, projection_name in (
        ("/api/v1/private-api-status", "PrivateApiStatus"),
        ("/api/v1/workplace-library", "WorkplaceLibrary"),
        ("/api/v1/workplace-access", "InitialAccessStatus"),
        ("/api/v1/work-session-transcript/{session_id}", "WorkSessionTranscript"),
        ("/api/v1/workplace-file/{file_id}", "WorkplaceFileContent"),
        ("/api/v1/egress-log/{day}", "EgressLog"),
        ("/api/v1/harness-status", "HarnessStatus"),
        ("/api/v1/api-limits", "ApiLimits"),
        ("/api/v1/laboratory-catalogue", "LaboratoryCatalogue"),
        ("/api/v1/laboratory-runs", "LaboratoryRuns"),
        ("/api/v1/settings-transfer-status/{operation_id}", "SettingsTransferStatus"),
        ("/api/v1/persona-markdown/{persona_id}", "ProfileMarkdown"),
    ):
        params = list(scope_parameters)
        if "{persona_id}" in route:
            params.append(
                {
                    "name": "persona_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
                }
            )
        if "{file_id}" in route:
            params.append(
                {
                    "name": "file_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
                }
            )
        if "{session_id}" in route:
            params.append(
                {
                    "name": "session_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
                }
            )
        if "{day}" in route:
            params.append(
                {
                    "name": "day",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                }
            )
        if "{operation_id}" in route:
            params.append(
                {
                    "name": "operation_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
                }
            )
        if projection_name == "LaboratoryRuns":
            params.extend(
                [
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                    {
                        "name": "cursor",
                        "in": "query",
                        "schema": {"type": "string", "maxLength": 128},
                    },
                ]
            )
        paths[route] = {
            "get": {
                "operationId": "read_" + projection_name,
                "security": operator,
                "parameters": params,
                "responses": {"200": response(projection_name), **errors},
            }
        }
    paths["/api/v1/personas/{persona_id}/validate"] = {
        "post": {
            "operationId": "validate_profile",
            "security": writing,
            "parameters": [
                {
                    "name": "persona_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
                }
            ],
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": ref("EditCommand")}},
            },
            "responses": {"200": response("ProfileValidation"), **errors},
        }
    }
    paths["/api/v1/laboratory/transfer-preview"] = {
        "post": {
            "operationId": "preview_settings_transfer",
            "security": writing,
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": ref("EditCommand")}},
            },
            "responses": {"200": response("SettingsTransferPreview"), **errors},
        }
    }
    from ai_agent_contracts.panel import (
        ChangeDecisionArguments,
        ChangeInterpretationArguments,
        ConfigureGM,
        LaboratoryCreateArguments,
        LaboratoryEventArguments,
        LaboratoryStartArguments,
        LaboratoryStopArguments,
        PersonaApplyArguments,
        PersonaCopyArguments,
        PersonaCreateArguments,
        PersonaValidateArguments,
        ResourceArguments,
        SettingsTransferArguments,
    )

    payloads = {
        name + "@1.0": ref(model.__name__)
        for name, model in (
            ("persona.create-args", PersonaCreateArguments),
            ("persona.validate-args", PersonaValidateArguments),
            ("persona.apply-args", PersonaApplyArguments),
            ("persona.copy-args", PersonaCopyArguments),
            ("gm.configure-args", ConfigureGM),
            ("change.decision-args", ChangeDecisionArguments),
            ("change.interpretation-args", ChangeInterpretationArguments),
            ("change.rollback-args", ResourceArguments),
            ("opportunity.cancel-args", ResourceArguments),
            ("arc.pause-args", ResourceArguments),
            ("laboratory.transfer-settings-args", SettingsTransferArguments),
            ("laboratory.start-args", LaboratoryStartArguments),
            ("laboratory.stop-args", LaboratoryStopArguments),
            ("laboratory.event-args", LaboratoryEventArguments),
            ("laboratory.create-args", LaboratoryCreateArguments),
        )
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "AI Agent Platform", "version": "1.0"},
        "paths": paths,
        "components": {
            "schemas": definitions,
            "parameters": {item["name"]: item for item in scope_parameter_definitions},
            "responses": {"ApiError": response("ApiError", "Safe contract error")},
            "securitySchemes": {
                "OperatorSession": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": "ai_operator_session",
                },
                "CSRF": {"type": "apiKey", "in": "header", "name": "X-CSRF-Token"},
                "ServiceIdentity": {"type": "http", "scheme": "bearer"},
            },
        },
        "x-runtime-implemented": False,
        "x-limits": {
            "body_bytes": MAX_BODY_BYTES,
            "text_chars": MAX_TEXT_CHARS,
            "list_items": MAX_LIST_ITEMS,
            "stream_buffer_events": 256,
        },
        "x-websocket": {
            "path": "/api/v1/events",
            "security": operator,
            "origin_check": True,
            "scope_check": True,
            "subscription": ref("EventSubscription"),
            "messages": ref("StreamEvent"),
            "gap_requires_projection": True,
        },
        "x-extension-schemas": {
            **payloads,
            **(extensions.schemas() if extensions is not None else {}),
        },
    }


def _ts(schema: Schema) -> str:
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[-1]
    if "const" in schema:
        return json.dumps(schema["const"], ensure_ascii=False)
    if "enum" in schema:
        return " | ".join(json.dumps(item, ensure_ascii=False) for item in schema["enum"])
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return " | ".join("(" + _ts(item) + ")" for item in schema[key])
    if "allOf" in schema:
        return " & ".join("(" + _ts(item) + ")" for item in schema["allOf"])
    kind = schema.get("type")
    if kind == "array":
        return "Array<" + _ts(schema.get("items", {})) + ">"
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        members = [
            json.dumps(name) + ("" if name in required else "?") + ": " + _ts(value) + ";"
            for name, value in sorted(properties.items())
        ]
        additional = schema.get("additionalProperties", False)
        if isinstance(additional, dict):
            members.append("[key: string]: " + _ts(additional) + ";")
        elif additional is True or (not properties and additional is not False):
            members.append("[key: string]: unknown;")
        return "{ " + " ".join(members) + " }"
    simple = {
        "string": "string",
        "integer": "number",
        "number": "number",
        "boolean": "boolean",
        "null": "null",
    }
    if kind in simple:
        return simple[kind]
    if schema == {}:
        return "unknown"
    raise ValueError("UNSUPPORTED_TS_SCHEMA")


def typescript() -> str:
    roots, definitions = schema_bundle()
    output = dict(definitions)
    for name, schema in roots.items():
        if name not in output:
            output[name] = schema
    return (
        "// Generated from Pydantic contracts 1.0. Do not edit.\n"
        "// UTC/decimal strings and bounds are validated on the server, not by TS.\n"
        + "\n".join(
            "export type " + name + " = " + _ts(schema) + ";"
            for name, schema in sorted(output.items())
        )
        + "\n"
    )


def artifacts() -> dict[str, str]:
    return {
        "contracts.schema.json": json.dumps(
            json_schema(), ensure_ascii=False, sort_keys=True, indent=2
        )
        + "\n",
        "openapi.json": json.dumps(openapi(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        "contracts.ts": typescript(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        if any(
            not (args.output / name).is_file()
            or (args.output / name).read_text(encoding="utf-8") != content
            for name, content in artifacts().items()
        ):
            raise SystemExit("Contract export differs; regenerate it.")
    else:
        args.output.mkdir(parents=True, exist_ok=True)
        for name, content in artifacts().items():
            (args.output / name).write_text(content, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
