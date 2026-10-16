"""Use accepted repository ACL/TTL/privacy filters; no second data owner."""

import base64
import hashlib
import json
from datetime import datetime
from typing import cast

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.storage import Principal
from ai_agent_contracts.api import Page
from ai_agent_contracts.catalogue import READ_MODELS
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import UTC, Contract, Id, Record, Scope
from ai_agent_contracts.states import ErrorCode
from pydantic import TypeAdapter

CHRONOLOGICAL = frozenset({"sources", "utterances", "deliveries", "operations"})


def cursor_binding(resource: str, scope: Scope, utterance_id: str | None) -> str:
    return hashlib.sha256(
        json.dumps([resource, scope.partition, utterance_id]).encode()
    ).hexdigest()[:32]


def encode_cursor(binding: str, moment: datetime, identifier: str) -> str:
    raw = json.dumps([binding, moment.isoformat(), identifier], separators=(",", ":")).encode()
    return "t1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str, binding: str) -> tuple[datetime, str]:
    try:
        if not value.startswith("t1.") or len(value) > 512:
            raise ValueError()
        parsed = json.loads(
            base64.b64decode(value[3:] + "=" * (-len(value[3:]) % 4), altchars=b"-_", validate=True)
        )
        if not isinstance(parsed, list) or len(parsed) != 3 or parsed[0] != binding:
            raise ValueError()
        moment = TypeAdapter(UTC).validate_json(json.dumps(parsed[1]))
        identifier = TypeAdapter(Id).validate_python(parsed[2])
        return moment, identifier
    except ValueError, TypeError, UnicodeError:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None


class SQLiteReader:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    async def read(
        self,
        resource: str,
        principal_id: str,
        scope: Scope,
        resource_id: str | None,
        limit: int,
        cursor: str | None,
        order: str | None = None,
        utterance_id: str | None = None,
    ) -> Contract | None:
        model = READ_MODELS.get(resource)
        if model is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        repository = self.store.repository(
            cast(type[Record], model), Principal(principal_id, "operator"), scope
        )
        if resource_id is not None:
            return await repository.get(resource_id, purpose="api_transfer")
        if order == "desc":
            if resource not in CHRONOLOGICAL or (
                utterance_id is not None and resource != "deliveries"
            ):
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            binding = cursor_binding(resource, scope, utterance_id)
            before, before_id = decode_cursor(cursor, binding) if cursor else (None, None)
            rows = await repository.recent(
                limit=limit,
                before=before,
                before_id=before_id,
                chronology=True,
                utterance_id=utterance_id,
                purpose="api_transfer",
            )
            complete = len(rows) < limit
            last = rows[-1] if rows else None
            return Page[model](  # type: ignore[valid-type]
                items=rows,
                limit=limit,
                complete=complete,
                next_cursor=encode_cursor(
                    binding, getattr(last, "occurred_at", last.created_at), last.id
                )
                if last is not None and not complete
                else None,
            )
        rows = await repository.list(limit=limit, after_id=cursor, purpose="api_transfer")
        complete = len(rows) < limit
        return Page[model](  # type: ignore[valid-type]
            items=rows,
            limit=limit,
            next_cursor=None if complete else rows[-1].id,
            complete=complete,
        )
