"""M5-04: local quality/usage/resource evaluation of the platform over twenty scenarios.

Dry-run (canned stand-in) is always available; a real model needs an explicit ``Authorisation``.
The operator scores quality 1-5 against the doc22 rubric; this package never does.
"""

from .budget import Authorisation, BudgetGuard, Price, RealRunRefused
from .catalogue import CatalogueError, quality_catalogue
from .harness import DEFAULT_REPEATS, Evaluation, character_assignment
from .model import OPERATOR, QualityScenario
from .providers import DryModel, Recorder
from .report import EvaluationReport, RunRecord, summarise_saved
from .rubric import CRITERIA, TARGET_MEDIAN, ScoreError, Summary, parse_scores, summarise

__all__ = [
    "CRITERIA",
    "DEFAULT_REPEATS",
    "OPERATOR",
    "TARGET_MEDIAN",
    "Authorisation",
    "BudgetGuard",
    "CatalogueError",
    "DryModel",
    "Evaluation",
    "EvaluationReport",
    "Price",
    "QualityScenario",
    "RealRunRefused",
    "Recorder",
    "RunRecord",
    "ScoreError",
    "Summary",
    "character_assignment",
    "parse_scores",
    "quality_catalogue",
    "summarise",
    "summarise_saved",
]
