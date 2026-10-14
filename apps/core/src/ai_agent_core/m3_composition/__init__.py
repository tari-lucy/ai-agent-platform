"""Explicit stopped M3 composition; no implicit schema, grants or provider IO."""

from ai_agent_contracts.extensions import ExtensionRegistry


def new_registry() -> ExtensionRegistry:
    """Register accepted payload models only; no storage or external IO."""
    from ai_agent_application.development import register_development_extensions
    from ai_agent_application.llm import register_llm_extensions
    from ai_agent_application.persona import register_persona_extensions
    from ai_agent_application.social import register_social_extensions

    from .schemas import register

    registry = ExtensionRegistry()
    register_persona_extensions(registry)
    register_social_extensions(registry)
    register_development_extensions(registry)
    register_llm_extensions(registry)
    register(registry)
    return registry
