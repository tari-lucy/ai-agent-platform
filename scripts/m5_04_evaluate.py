"""M5-04 evaluation CLI: list scenarios, dry-run, apply the operator's scores.

``dry-run`` uses a canned stand-in: no network, no key, no paid call. A real-model run is not
enabled here; it needs the operator's explicit authorisation and a provider wiring that is added
only after that. Reports go to a local ignored directory (``artifacts/``), never to Git.
"""

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
for source in (
    "apps/core/src",
    "apps/worker/src",
    "packages/contracts/src",
    "packages/domain/src",
    "packages/application/src",
    "packages/adapters/src",
):
    sys.path.insert(0, str(ROOT / source))

from ai_agent_adapters.recall import SQLiteRecallIndex  # noqa: E402
from ai_agent_adapters.storage.database import SQLiteStore  # noqa: E402
from ai_agent_application.evaluation import (  # noqa: E402
    CRITERIA,
    DEFAULT_REPEATS,
    Evaluation,
    EvaluationReport,
    Price,
    ScoreError,
    quality_catalogue,
    summarise_saved,
)
from ai_agent_application.laboratory import LabRunner, VirtualClock, lab_grants  # noqa: E402
from ai_agent_contracts.common import Scope  # noqa: E402
from ai_agent_contracts.extensions import ExtensionRegistry  # noqa: E402

DEFAULT_OUT = ROOT / "artifacts" / "m5-04"


async def dry_run(selected: list[str], repeats: int, price: Price | None) -> EvaluationReport:
    from ai_agent_application.development import register_development_extensions
    from ai_agent_application.persona import register_persona_extensions
    from ai_agent_application.social import register_social_extensions

    registry = ExtensionRegistry()
    register_persona_extensions(registry)
    register_social_extensions(registry)
    register_development_extensions(registry)
    clock = VirtualClock()
    with tempfile.TemporaryDirectory(prefix="m5-04-") as base_name:
        base = Path(base_name)
        store = SQLiteStore(base / "store.sqlite", extensions=registry, clock=clock)
        await store.upgrade(ROOT / "migrations")
        counter = iter(range(1, 10_000))

        async def scope_factory(label: str) -> Scope:
            scope = Scope(
                workspace_id="synthetic-workspace",
                persona_id="persona-eval",
                memory_space_id=f"space-test-{next(counter)}",
                memory_mode="test",
            )
            await store.provision(scope, lab_grants())
            return scope

        runner = LabRunner(
            storage=store,
            scope_factory=scope_factory,
            index_factory=lambda: SQLiteRecallIndex(base / f"{uuid4()}-index.sqlite"),
            clock=clock,
        )
        scenarios = [s for s in quality_catalogue() if not selected or s.scenario_id in selected]
        return await Evaluation.dry(runner, price=price).evaluate(scenarios, repeats=repeats)


def command_catalogue() -> int:
    names = dict(CRITERIA)
    for s in quality_catalogue():
        focus = ", ".join(names[k] for k in s.focus) or "только жёсткие проверки"
        print(
            f"{s.scenario_id}  [{s.origin:9}] {s.title}  (сессий {s.sessions}, реплик {s.messages})"
        )
        print(f"        оценивать: {focus}")
    return 0


def command_dry_run(args: argparse.Namespace) -> int:
    price = None
    if args.price_input is not None and args.price_output is not None:
        price = Price(args.currency, Decimal(args.price_input), Decimal(args.price_output))
    selected = [x for x in args.only.split(",") if x] if args.only else []
    known = {s.scenario_id for s in quality_catalogue()}
    if not set(selected) <= known:
        print("неизвестный сценарий в --only", file=sys.stderr)
        return 2
    report = asyncio.run(dry_run(selected, args.repeats, price))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out or DEFAULT_OUT / f"dry-{stamp}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(report.to_json(), encoding="utf-8")
    (out / "report.md").write_text(report.to_markdown(), encoding="utf-8")
    (out / "scores.template.json").write_text(
        json.dumps(report.score_sheet(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    failed = report.hard_failed()
    usage = report.usage()
    print(f"dry-run (mock): прогонов {len(report.runs)}, с жёстким отказом {len(failed)}")
    print(
        f"вызовов {usage['provider_calls']}, токены (оценка) "
        f"{usage['input_tokens_known']}+{usage['output_tokens_known']}"
    )
    print(f"отчёт: {out}")
    return 1 if failed else 0


def command_summary(args: argparse.Namespace) -> int:
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    sheet = json.loads(Path(args.scores).read_text(encoding="utf-8"))
    try:
        summary = summarise_saved(report, sheet)
    except ScoreError as error:
        print(f"лист оценок не принят: {error}", file=sys.stderr)
        return 2
    if report["mock"]:
        print("ВНИМАНИЕ: отчёт построен на заглушке; оценки не говорят о реальной модели.")
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("catalogue", help="список 20 сценариев")
    dry = sub.add_parser("dry-run", help="прогон на заглушке, без сети")
    dry.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    dry.add_argument("--only", help="например Q-01,Q-12")
    dry.add_argument("--out", help="каталог отчёта (по умолчанию artifacts/m5-04/...)")
    dry.add_argument("--currency", default="USD")
    dry.add_argument(
        "--price-input", help="цена входа за 1M токенов: только предварительный расчёт"
    )
    dry.add_argument("--price-output", help="цена выхода за 1M токенов")
    summary = sub.add_parser("summary", help="медианы по листу оценок оператора")
    summary.add_argument("--report", required=True)
    summary.add_argument("--scores", required=True)
    sub.add_parser("real", help="не включено: нужны явное разрешение и бюджет оператора")
    args = parser.parse_args()
    if args.command == "catalogue":
        return command_catalogue()
    if args.command == "dry-run":
        return command_dry_run(args)
    if args.command == "summary":
        return command_summary(args)
    print("real: не включено. Нужны разрешение оператора, потолок бюджета и привязка модели.")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
