"""Scenario report: deterministic log, hard-invariant findings and a replay digest."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from ai_agent_contracts.common import Scope, SourceRef

type Status = Literal["passed", "failed", "error"]


@dataclass(frozen=True)
class Finding:
    name: str
    ok: bool
    detail: str = ""
    hard: bool = True


@dataclass
class ScenarioReport:
    scenario_id: str
    scenario_version: int
    seed: str
    started_at: datetime
    finished_at: datetime
    status: Status
    findings: tuple[Finding, ...]
    log: tuple[dict[str, Any], ...]
    model_calls: int
    error: str = ""
    mock: bool = True
    digest: str = field(default="")
    # None means legacy/unproven; an explicit empty tuple means a source-free report.
    source_refs: tuple[SourceRef, ...] | None = None
    scope: Scope | None = None

    def __post_init__(self) -> None:
        self.digest = compute_digest(self)

    @property
    def failed(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if not f.ok)

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def as_dict(self) -> dict[str, Any]:
        lineage = (
            {}
            if self.source_refs is None
            else {
                "source_refs": [r.model_dump(mode="json") for r in self.source_refs],
                "source_ids": sorted({r.source_id for r in self.source_refs}),
            }
        )
        return {
            **lineage,
            **({"scope": self.scope.model_dump(mode="json")} if self.scope else {}),
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "seed": self.seed,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "status": self.status,
            "mock": self.mock,
            "model_calls": self.model_calls,
            "error": self.error,
            "findings": [vars(f) for f in self.findings],
            "log": list(self.log),
            "digest": self.digest,
            "note": "Mock scenario: checks interfaces, invariants and failures, not LLM quality.",
        }


def compute_digest(report: ScenarioReport) -> str:
    body = {
        "id": report.scenario_id,
        "version": report.scenario_version,
        "seed": report.seed,
        "start": report.started_at.isoformat(),
        "end": report.finished_at.isoformat(),
        "status": report.status,
        "findings": [vars(f) for f in report.findings],
        # Replay digest describes the scenario, not physical identities of a fresh TEST world.
        # ArtifactRef.checksum separately authenticates the complete body, including lineage.
        "log": [{k: v for k, v in row.items() if k != "source_id"} for row in report.log],
        "calls": report.model_calls,
        "error": report.error,
    }
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
