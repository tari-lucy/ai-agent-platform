"""Stable, transport-independent contract namespace. Wire catalogue: catalogue.ADAPTERS."""

from ai_agent_contracts.codec import ContractError, from_wire, to_wire
from ai_agent_contracts.common import SCHEMA_VERSION, Contract, Scope
from ai_agent_contracts.extensions import ExtensionRegistry

__all__ = [
    "SCHEMA_VERSION",
    "Contract",
    "ContractError",
    "ExtensionRegistry",
    "Scope",
    "from_wire",
    "to_wire",
]
