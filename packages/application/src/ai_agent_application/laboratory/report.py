"""Scenario report: deterministic log, hard-invariant findings and a replay digest."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

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

    def __post_init__(self) -> None:
        self.digest = compute_digest(self)

    @property
    def failed(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if not f.ok)

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def as_dict(self) -> dict[str, Any]:
        return {
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
        "log": list(report.log),
        "calls": report.model_calls,
        "error": report.error,
    }
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
