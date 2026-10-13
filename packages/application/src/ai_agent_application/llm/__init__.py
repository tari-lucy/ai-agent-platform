"""LLM gateway (M2-04): roles, bounded retry, cancellation and honest attempt reports."""

from .model import (
    AttemptReport,
    ChatMessage,
    Delta,
    Failure,
    Finished,
    Generation,
    GenerationParameters,
    GenerationRequest,
    ProviderCall,
    ProviderError,
    ResolvedAssignment,
    Started,
    UsageReported,
)
from .ports import Admission, AttemptSink, LLMProvider, ProviderFactory, register_llm_extensions
from .roles import resolve_assignment
from .service import LLMGateway, parse_result

__all__ = [
    "Admission",
    "AttemptReport",
    "AttemptSink",
    "ChatMessage",
    "Delta",
    "Failure",
    "Finished",
    "Generation",
    "GenerationParameters",
    "GenerationRequest",
    "LLMGateway",
    "LLMProvider",
    "ProviderCall",
    "ProviderError",
    "ProviderFactory",
    "ResolvedAssignment",
    "Started",
    "UsageReported",
    "parse_result",
    "register_llm_extensions",
    "resolve_assignment",
]
