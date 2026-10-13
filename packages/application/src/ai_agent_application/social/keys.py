"""Stable identities. Names and wording never identify people, topics or promises."""

import re
import unicodedata

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode

from ai_agent_application.memory.service import derived_id

__all__ = ["derived_id", "normalize_topic_key", "person_id_for", "topic_id_for"]


def normalize_topic_key(key: str) -> str:
    """Structured semantic key; rewording the question text does not change it."""
    folded = unicodedata.normalize("NFC", key).casefold()
    slug = re.sub(r"[^\w]+", "-", folded).strip("-_")[:100]
    if not slug:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    return slug


def person_id_for(scope: Scope, platform_id: str, account_id: str) -> str:
    """First-seen anchor id; stays valid even if the display name changes."""
    return derived_id("person", scope, platform_id, account_id)


def topic_id_for(scope: Scope, person_id: str, topic_key: str) -> str:
    return derived_id("topic", scope, person_id, normalize_topic_key(topic_key))
