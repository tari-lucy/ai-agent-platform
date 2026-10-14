"""Evaluation report: per-run records, score sheet for the operator, honest separation of kinds.

Mock (dry-run) and real calls are never mixed in one number. A dry-run report states that it says
nothing about model quality; the summary exists only after the operator's scores are applied.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .model import QualityScenario
from .rubric import CRITERIA, Summary, parse_scores, summarise

NOT_MEASURED_RECALL = "не измерено здесь: отдельные пробы recall (M0-05)"


@dataclass
class RunRecord:
    scenario_id: str
    repeat: int
    seed: str
    status: str  # passed | failed | error | skipped
    mock: bool
    model: str
    hard_failures: list[str] = field(default_factory=list)
    soft_failures: list[str] = field(default_factory=list)
    error: str = ""
    skipped_reason: str = ""
    digest: str = ""
    turns: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    cpu_seconds: float = 0.0

    @property
    def unknown_usage_calls(self) -> int:
        return sum(1 for c in self.calls if c["input_tokens"] is None and not c["failure"])

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass
class EvaluationReport:
    mode: str  # "dry-run (mock)" | "real"
    mock: bool
    model: str
    started_at: datetime
    repeats: int
    scenarios: list[QualityScenario]
    runs: list[RunRecord]
    budget: dict[str, Any]
    resources: dict[str, Any]
    stopped_reason: str = ""

    # ---- numbers ---------------------------------------------------------------------

    def expected(self) -> list[tuple[str, int]]:
        return [(s.scenario_id, n) for s in self.scenarios for n in range(1, self.repeats + 1)]

    def hard_failed(self) -> list[RunRecord]:
        return [r for r in self.runs if r.status in ("failed", "error")]

    def latency_ms(self) -> dict[str, float | None]:
        values = sorted(c["latency_ms"] for r in self.runs for c in r.calls if not c["failure"])
        if not values:
            return {"p50": None, "p95": None, "max": None}
        return {
            "p50": values[len(values) // 2],
            "p95": values[min(len(values) - 1, int(len(values) * 0.95))],
            "max": values[-1],
        }

    def usage(self) -> dict[str, Any]:
        calls = [c for r in self.runs for c in r.calls]
        done = [c for c in calls if not c["failure"]]
        return {
            "provider_calls": len(calls),
            "failed_or_refused_calls": len(calls) - len(done),
            "unknown_usage_calls": sum(1 for c in done if c["input_tokens"] is None),
            "input_tokens_known": sum(c["input_tokens"] or 0 for c in done),
            "output_tokens_known": sum(c["output_tokens"] or 0 for c in done),
            "usage_sources": sorted({c["usage_source"] for c in done}),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "m5-04-evaluation",
            "mode": self.mode,
            "mock": self.mock,
            "model": self.model,
            "started_at": self.started_at.isoformat(),
            "repeats": self.repeats,
            "scenarios": [
                {
                    "id": s.scenario_id,
                    "slug": s.slug,
                    "title": s.title,
                    "origin": s.origin,
                    "sessions": s.sessions,
                    "messages": s.messages,
                    "focus": list(s.focus),
                    "expect": s.expect,
                    "failure": s.failure,
                }
                for s in self.scenarios
            ],
            "runs": [r.as_dict() for r in self.runs],
            "usage": self.usage(),
            "budget": self.budget,
            "resources": self.resources | {"latency_ms": self.latency_ms()},
            "recall": NOT_MEASURED_RECALL,
            "stopped_reason": self.stopped_reason or None,
            "hard_failed_runs": [f"{r.scenario_id}#{r.repeat}" for r in self.hard_failed()],
            "quality": (
                "НЕ ОЦЕНЕНО: это dry-run на заглушке; качество реальной модели не измерялось."
                if self.mock
                else "Ждёт оценок оператора по шкале 1-5 (см. лист оценок)."
            ),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    # ---- operator score sheet ----------------------------------------------------------

    def score_sheet(self) -> dict[str, Any]:
        """Empty sheet: the operator fills ``criteria`` (1-5, null = n/a) and ``cycle``."""
        focus = {s.scenario_id: s.focus for s in self.scenarios}
        return {
            "kind": "m5-04-scores",
            "mock": self.mock,
            "model": self.model,
            "scale": "1 (плохо) .. 5 (отлично); null = критерий к прогону не относится",
            "cycle": "true, если в прогоне виден повторяющийся цикл",
            "scores": [
                {
                    "scenario": r.scenario_id,
                    "repeat": r.repeat,
                    "criteria": dict.fromkeys(focus[r.scenario_id]),
                    "cycle": False,
                    "note": "",
                }
                for r in self.runs
                if r.status != "skipped" and focus[r.scenario_id]
            ],
        }

    def summary(self, sheet: dict[str, Any]) -> Summary:
        focus = {s.scenario_id: s.focus for s in self.scenarios}
        return summarise(parse_scores(sheet), self.expected(), focus)

    # ---- human-readable ----------------------------------------------------------------

    def to_markdown(self) -> str:
        names = dict(CRITERIA)
        out = [f"# Оценка M5-04: {self.mode}", ""]
        if self.mock:
            out += [
                "> **Это dry-run на заглушке.** Ответы заготовлены, реальная модель не вызывалась,",
                "> качество не измерялось. Отчёт проверяет только прогон, учёт и лист оценок.",
                "",
            ]
        usage = self.usage()
        resources = self.resources | {"latency_ms": self.latency_ms()}
        out += [
            f"- Модель: `{self.model}`; сценариев: {len(self.scenarios)}; повторов: {self.repeats}",
            f"- Прогонов: {len(self.runs)}; с жёстким отказом: {len(self.hard_failed())}",
            f"- Вызовов провайдера: {usage['provider_calls']}; без известного usage: "
            f"{usage['unknown_usage_calls']}",
            f"- Токены (известные): вход {usage['input_tokens_known']}, "
            f"выход {usage['output_tokens_known']}; "
            f"источники: {', '.join(usage['usage_sources']) or '—'}",
            f"- Бюджет: {json.dumps(self.budget, ensure_ascii=False)}",
            f"- Ресурсы: {json.dumps(resources, ensure_ascii=False)}",
            f"- Recall: {NOT_MEASURED_RECALL}",
        ]
        if self.stopped_reason:
            out.append(f"- **Остановлено:** {self.stopped_reason}")
        by_id = {s.scenario_id: s for s in self.scenarios}
        for run in self.runs:
            scenario = by_id[run.scenario_id]
            out += ["", f"## {scenario.scenario_id} · {scenario.title} · повтор {run.repeat}", ""]
            out.append(
                f"Статус: **{run.status}**"
                + (f" ({run.skipped_reason})" if run.skipped_reason else "")
            )
            out.append(f"Ожидается: {scenario.expect}")
            out.append(f"Неуспех: {scenario.failure}")
            if scenario.focus:
                out.append("Оценить: " + ", ".join(names[k] for k in scenario.focus))
            for failure in run.hard_failures:
                out.append(f"- ЖЁСТКАЯ ПРОВЕРКА НЕ ПРОЙДЕНА: {failure}")
            for failure in run.soft_failures:
                out.append(f"- мягкая находка: {failure}")
            out.append("")
            for turn in run.turns:
                reply = (
                    turn["reply"]
                    if turn["reply"] is not None
                    else "— нет ответа (" + str(turn["status"] or turn["reason"] or "закрыто") + ")"
                )
                out.append(f"- [{turn['who']}, сессия {turn['session']}] {turn['text']}")
                out.append(f"  → {reply}")
        return "\n".join(out) + "\n"


def summarise_saved(report: dict[str, Any], sheet: dict[str, Any]) -> Summary:
    """Apply the operator's sheet to a saved ``report.json`` (the CLI path)."""
    focus = {item["id"]: tuple(item["focus"]) for item in report["scenarios"]}
    expected = [
        (item["id"], n) for item in report["scenarios"] for n in range(1, report["repeats"] + 1)
    ]
    # a run that never happened (budget stop) cannot be scored; do not wait for it
    done = {(r["scenario_id"], r["repeat"]) for r in report["runs"] if r["status"] != "skipped"}
    return summarise(parse_scores(sheet), [e for e in expected if e in done], focus)
