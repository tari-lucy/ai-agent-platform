"""Social memory owner (M2-03): people, encounters, relationships, topics, promises. Not wired."""

from .keys import normalize_topic_key, person_id_for, topic_id_for
from .relationships import AspectLimit
from .schemas import RelationshipAspects, register_social_extensions
from .service import (
    EncounterResult,
    Identified,
    Obligation,
    PersonContext,
    QuestionDecision,
    SocialPolicy,
    SocialService,
)

__all__ = [
    "AspectLimit",
    "EncounterResult",
    "Identified",
    "Obligation",
    "PersonContext",
    "QuestionDecision",
    "RelationshipAspects",
    "SocialPolicy",
    "SocialService",
    "normalize_topic_key",
    "person_id_for",
    "register_social_extensions",
    "topic_id_for",
]
