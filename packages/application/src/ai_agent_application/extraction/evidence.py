"""What a source can prove. Decided from stored provenance, never from what the model claims.

Strength: canon/observed (3) > reported (2) > hypothesis (1) > interpretation (0). Words of a
participant prove that they said it (``reported``), a direct observation proves the observation,
canon only comes from an operator-confirmed canon source. Anything the character or a model produced
itself (its utterances, summaries, reflections, diary, posts, model proposals) is *self-derived*:
it can be cited, but it never counts as independent evidence and never lifts a claim above
``interpretation``.
"""

from dataclasses import dataclass

from ai_agent_contracts.common import Scope
from ai_agent_contracts.memory import Source
from ai_agent_contracts.states import Provenance

SELF_SOURCE_TYPES = frozenset(
    {"character-utterance", "summary", "reflection", "diary", "post", "consolidation"}
)
STRENGTH = {"canon": 3, "observed": 3, "reported": 2, "hypothesis": 1, "interpretation": 0}
CEILING_ONE_GROUP = {
    "canon": 0.95,
    "observed": 0.9,
    "reported": 0.6,
    "hypothesis": 0.4,
    "interpretation": 0.4,
}
CEILING_CORROBORATED = {**CEILING_ONE_GROUP, "observed": 0.95, "reported": 0.8}


@dataclass(frozen=True)
class SourceEvidence:
    max_type: str
    independent: bool
    reject: str | None = None


def classify(source: Source, scope: Scope) -> SourceEvidence:
    if source.source_type in SELF_SOURCE_TYPES or source.provenance in (
        Provenance.MODEL_PROPOSAL,
        Provenance.INTERPRETATION,
    ):
        return SourceEvidence("interpretation", independent=False)
    if source.provenance is Provenance.SIMULATION:
        if scope.memory_mode != "test":
            return SourceEvidence("interpretation", independent=False, reject="SIMULATION_IN_WORK")
        return SourceEvidence("observed", independent=True)
    if source.provenance is Provenance.OBSERVATION:
        return SourceEvidence("observed", independent=True)
    if source.provenance is Provenance.OPERATOR_INPUT and source.source_type == "operator-canon":
        return SourceEvidence("canon", independent=True)
    return SourceEvidence("reported", independent=True)  # third party or operator statement


def ceiling(knowledge_type: str, groups: int) -> float:
    table = CEILING_CORROBORATED if groups >= 2 else CEILING_ONE_GROUP
    return table[knowledge_type]
