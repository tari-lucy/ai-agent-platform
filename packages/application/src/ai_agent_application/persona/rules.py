"""Cross-field profile rules, revision diff and canon/state compatibility. Pure functions."""

from .model import Issue, ParsedProfile, ProfileDiff
from .schemas import STATE_EXCLUDED_LAYERS, PropertyDefinition, ValueMap


def _value_issue(definition: PropertyDefinition, value: float | str, code: str) -> Issue | None:
    section = definition.name
    if definition.kind == "scale":
        if isinstance(value, str):
            return Issue(code, "Для шкалы нужно число.", None, section)
        assert definition.minimum is not None and definition.maximum is not None
        if not definition.minimum <= value <= definition.maximum:
            return Issue(code, "Значение вне диапазона шкалы.", None, section)
        return None
    if not isinstance(value, str):
        return Issue(code, "Для текстового свойства нужна строка.", None, section)
    assert definition.max_chars is not None
    if len(value) > definition.max_chars:
        return Issue(code, "Текст длиннее предела свойства.", None, section)
    return None


def check_profile(profile: ParsedProfile) -> tuple[Issue, ...]:
    """Consistency of definitions, initial values and development bounds."""
    issues: list[Issue] = []
    defined = {item.name: item for item in profile.definitions.properties}
    values = profile.initial_values.values
    for name in values:
        if name not in defined:
            issues.append(
                Issue("UNKNOWN_PROPERTY", "Значение для неописанного свойства.", None, name)
            )
    for name, described in defined.items():
        if name not in values:
            issues.append(Issue("MISSING_INITIAL", "Нет начального значения.", None, name))
        elif (found := _value_issue(described, values[name], "INITIAL_INVALID")) is not None:
            issues.append(found)
    for bound in profile.bounds.bounds:
        definition = defined.get(bound.name)
        if definition is None:
            issues.append(
                Issue("UNKNOWN_PROPERTY", "Граница для неописанного свойства.", None, bound.name)
            )
            continue
        if definition.layer == "identity" and bound.autonomous:
            issues.append(
                Issue(
                    "IDENTITY_AUTONOMY",
                    "Базовая идентичность меняется только решением оператора.",
                    None,
                    bound.name,
                )
            )
        steps = (bound.max_step_per_episode, bound.max_step_per_window)
        if definition.kind == "text":
            if any(step is not None for step in steps):
                issues.append(
                    Issue("BOUND_INVALID", "Шаги применимы только к шкале.", None, bound.name)
                )
            continue
        assert definition.minimum is not None and definition.maximum is not None
        span = definition.maximum - definition.minimum
        if bound.autonomous and (steps[0] is None or steps[1] is None):
            issues.append(
                Issue(
                    "BOUND_INVALID", "Автономная шкала требует оба предела шага.", None, bound.name
                )
            )
        if any(step is not None and step > span for step in steps):
            issues.append(Issue("BOUND_INVALID", "Шаг больше диапазона шкалы.", None, bound.name))
        if steps[0] is not None and steps[1] is not None and steps[1] < steps[0]:
            issues.append(
                Issue("BOUND_INVALID", "Предел за окно меньше предела за эпизод.", None, bound.name)
            )
    return tuple(issues)


def state_properties(profile: ParsedProfile) -> dict[str, PropertyDefinition]:
    """Properties held in the character state; relationship aspects belong to social memory."""
    return {
        item.name: item
        for item in profile.definitions.properties
        if item.layer not in STATE_EXCLUDED_LAYERS
    }


def initial_state_values(profile: ParsedProfile) -> ValueMap:
    wanted = state_properties(profile)
    return ValueMap(values={k: v for k, v in profile.initial_values.values.items() if k in wanted})


def state_blockers(profile: ParsedProfile, current: dict[str, float | str]) -> tuple[Issue, ...]:
    """Reasons a new revision cannot adopt existing state without an explicit migration."""
    wanted = state_properties(profile)
    issues: list[Issue] = []
    for name, value in current.items():
        definition = wanted.get(name)
        if definition is None:
            issues.append(
                Issue(
                    "STATE_MIGRATION_REQUIRED",
                    "Свойство с накопленным значением удалено или перенесено; нужна миграция.",
                    None,
                    name,
                )
            )
        elif _value_issue(definition, value, "STATE_MIGRATION_REQUIRED") is not None:
            issues.append(
                Issue(
                    "STATE_MIGRATION_REQUIRED",
                    "Накопленное значение не подходит новой схеме; нужна миграция.",
                    None,
                    name,
                )
            )
    return tuple(issues)


def merged_state_values(profile: ParsedProfile, current: dict[str, float | str]) -> ValueMap:
    """Existing values are kept; only properties new to the schema receive initial values."""
    initial = initial_state_values(profile).values
    merged = dict(current)
    for name, value in initial.items():
        merged.setdefault(name, value)
    return ValueMap(values=merged)


def diff_profiles(old: ParsedProfile, new: ParsedProfile) -> ProfileDiff:
    before = {item.name: item for item in old.definitions.properties}
    after = {item.name: item for item in new.definitions.properties}
    old_bounds = {item.name: item for item in old.bounds.bounds}
    new_bounds = {item.name: item for item in new.bounds.bounds}
    names = sorted(set(old.initial_values.values) | set(new.initial_values.values))
    return ProfileDiff(
        canon_changed=old.canon != new.canon,
        style_changed=old.style != new.style,
        properties_added=tuple(sorted(after.keys() - before.keys())),
        properties_removed=tuple(sorted(before.keys() - after.keys())),
        properties_changed=tuple(
            sorted(name for name in before.keys() & after.keys() if before[name] != after[name])
        ),
        initial_values_changed=tuple(
            name
            for name in names
            if old.initial_values.values.get(name) != new.initial_values.values.get(name)
        ),
        bounds_changed=tuple(
            sorted(
                name
                for name in old_bounds.keys() | new_bounds.keys()
                if old_bounds.get(name) != new_bounds.get(name)
            )
        ),
    )
