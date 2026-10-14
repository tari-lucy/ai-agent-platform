"""Evidence extraction and consolidation proposals (M3-01)."""

from .consolidator import Consolidator
from .evidence import SELF_SOURCE_TYPES, ceiling, classify
from .extractor import Extractor
from .model import ExtractionReport, ExtractionResult, Proposal

__all__ = [
    "Consolidator",
    "SELF_SOURCE_TYPES",
    "ExtractionReport",
    "ExtractionResult",
    "Extractor",
    "Proposal",
    "ceiling",
    "classify",
]
