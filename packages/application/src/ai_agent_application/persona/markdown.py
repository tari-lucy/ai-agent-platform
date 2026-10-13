"""Strict Markdown profile format ``ai-agent-persona-profile/1`` (import and canonical export).

Layout::

    <!-- ai-agent-persona-profile/1 -->      first non-blank line
    # Optional title                           one level-1 heading, presentational, ignored
    ## Канон                                    free Markdown text
    ## Стиль                                    free Markdown text
    ## Свойства                                 one ```json block: PropertyDefinitions
    ## Начальные значения                       one ```json block: ValueMap
    ## Границы развития                         one ```json block: DevelopmentBounds

Every section is required exactly once, in any order. Unknown level-2 headings, prose around the
JSON blocks and unknown fields are rejected instead of silently dropped. Text inside code fences
never starts a section. No YAML or other parser dependency is involved.
"""

import json
import re
import unicodedata
from typing import Any

from pydantic import TypeAdapter, ValidationError

from .model import Issue, ParsedProfile
from .schemas import (
    MAX_MARKDOWN_BYTES,
    MAX_MARKDOWN_CHARS,
    DevelopmentBounds,
    PropertyDefinitions,
    ValueMap,
)

FORMAT_MARKER = "<!-- ai-agent-persona-profile/1 -->"
CANON, STYLE = "Канон", "Стиль"
PROPERTIES, VALUES, BOUNDS = "Свойства", "Начальные значения", "Границы развития"
TEXT_SECTIONS = (CANON, STYLE)
JSON_SECTIONS = (PROPERTIES, VALUES, BOUNDS)
SECTION_ORDER = (CANON, STYLE, PROPERTIES, VALUES, BOUNDS)
MAX_SECTION_CHARS = 16_000

_HEADING = re.compile(r"^## +(.*?) *$")
_TITLE = re.compile(r"^# +\S")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FORBIDDEN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩﻿]")
_SECRETS = (
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b[ \t]*[:=][ \t]*\S{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\bhttps?://[^\s/@]+:[^\s/@]+@"),
)


def _duplicate_free(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _nonfinite(_: str) -> None:
    raise ValueError("NONFINITE_JSON")


def _normalise(text: str) -> str:
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n"))


def _scan(text: str, issues: list[Issue]) -> None:
    for number, line in enumerate(text.split("\n"), start=1):
        if _FORBIDDEN.search(line) or "\r" in line:
            issues.append(Issue("CONTROL_CHARACTER", "Недопустимый управляющий символ.", number))
        if any(pattern.search(line) for pattern in _SECRETS):
            issues.append(
                Issue(
                    "SECRET_LIKE", "Строка похожа на секрет; секреты не входят в профиль.", number
                )
            )


def _split_sections(text: str, issues: list[Issue]) -> dict[str, tuple[int, list[str]]]:
    """Return section name -> (first body line number, body lines)."""
    sections: dict[str, tuple[int, list[str]]] = {}
    current: list[str] | None = None
    marker_seen = False
    title_seen = False
    fence: tuple[str, int] | None = None
    for number, line in enumerate(text.split("\n"), start=1):
        if not marker_seen:
            if not line.strip():
                continue
            if line.strip() != FORMAT_MARKER:
                issues.append(
                    Issue("FORMAT_MARKER", "Первая строка должна быть маркером формата.", number)
                )
                return sections
            marker_seen = True
            continue
        opened = _FENCE.match(line)
        if fence is not None:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= fence[1]:
                if not opened.group(2).strip():
                    fence = None
        elif opened and not (opened.group(1)[0] == "`" and "`" in opened.group(2)):
            fence = (opened.group(1)[0], len(opened.group(1)))
        elif (heading := _HEADING.match(line)) is not None:
            name = heading.group(1)
            if name not in SECTION_ORDER:
                issues.append(Issue("UNKNOWN_SECTION", "Неизвестный раздел.", number))
                current = []  # body discarded: one finding per bad section, not per line
                continue
            if name in sections:
                issues.append(Issue("DUPLICATE_SECTION", "Раздел повторяется.", number, name))
                current = []
                continue
            current = []
            sections[name] = (number + 1, current)
            continue
        elif _TITLE.match(line) and current is None and not title_seen and not sections:
            title_seen = True
            continue
        if current is not None:
            current.append(line)
        elif line.strip():
            issues.append(Issue("STRAY_CONTENT", "Текст вне разделов.", number))
    if not marker_seen:
        issues.append(Issue("FORMAT_MARKER", "Нет маркера формата профиля.", 1))
    if fence is not None:
        issues.append(Issue("FENCE_UNCLOSED", "Не закрыт блок кода."))
    return sections


def _json_block(name: str, first: int, lines: list[str], issues: list[Issue]) -> Any | None:
    body: list[str] = []
    state = "before"
    for offset, line in enumerate(lines):
        number = first + offset
        if state == "before":
            if not line.strip():
                continue
            opened = _FENCE.match(line)
            if not opened or opened.group(2).strip().lower() != "json":
                issues.append(Issue("STRAY_CONTENT", "Ожидается блок ```json.", number, name))
                return None
            state = "inside"
            fence = opened.group(1)
        elif state == "inside":
            closing = _FENCE.match(line)
            if closing and closing.group(1)[0] == fence[0] and len(closing.group(1)) >= len(fence):
                if not closing.group(2).strip():
                    state = "after"
                    continue
            body.append(line)
        elif line.strip():
            issues.append(Issue("STRAY_CONTENT", "Текст после блока.", number, name))
            return None
    if state != "after":
        issues.append(Issue("JSON_BLOCK", "Нет полного блока ```json.", first, name))
        return None
    try:
        return json.loads(
            "\n".join(body), object_pairs_hook=_duplicate_free, parse_constant=_nonfinite
        )
    except ValueError, RecursionError:
        issues.append(Issue("JSON_INVALID", "Некорректный JSON.", first, name))
        return None


def _model[M](adapter: TypeAdapter[M], name: str, value: Any, issues: list[Issue]) -> M | None:
    try:
        return adapter.validate_python(value)
    except ValidationError as error:
        for item in error.errors(include_input=False, include_url=False)[:20]:
            where = ".".join(str(part) for part in item["loc"])
            issues.append(
                Issue("SCHEMA_INVALID", f"Поле {where or '<root>'}: {item['type']}.", None, name)
            )
        return None


def parse_markdown(markdown: str) -> tuple[ParsedProfile | None, tuple[Issue, ...]]:
    """Parse only; cross-field rules are applied by ``rules.check_profile``."""
    issues: list[Issue] = []
    if len(markdown) > MAX_MARKDOWN_CHARS or len(markdown.encode("utf-8", "replace")) > (
        MAX_MARKDOWN_BYTES
    ):
        return None, (Issue("TOO_LARGE", "Документ превышает предел размера."),)
    text = _normalise(markdown.removeprefix("﻿"))
    _scan(text, issues)
    sections = _split_sections(text, issues)
    for name in SECTION_ORDER:
        if name not in sections and not any(i.code == "FORMAT_MARKER" for i in issues):
            issues.append(Issue("MISSING_SECTION", "Нет обязательного раздела.", None, name))
    bodies: dict[str, str] = {}
    for name in TEXT_SECTIONS:
        if name in sections:
            first, lines = sections[name]
            body = "\n".join(lines).strip()
            if not body:
                issues.append(Issue("EMPTY_SECTION", "Раздел пуст.", first, name))
            elif len(body) > MAX_SECTION_CHARS:
                issues.append(Issue("TOO_LARGE", "Раздел превышает предел.", first, name))
            bodies[name] = body
    raw: dict[str, Any] = {}
    for name in JSON_SECTIONS:
        if name in sections:
            first, lines = sections[name]
            value = _json_block(name, first, lines, issues)
            if value is not None:
                raw[name] = value
    definitions = (
        _model(TypeAdapter(PropertyDefinitions), PROPERTIES, raw[PROPERTIES], issues)
        if PROPERTIES in raw
        else None
    )
    values = _model(TypeAdapter(ValueMap), VALUES, raw[VALUES], issues) if VALUES in raw else None
    bounds = (
        _model(TypeAdapter(DevelopmentBounds), BOUNDS, raw[BOUNDS], issues)
        if BOUNDS in raw
        else None
    )
    if issues or definitions is None or values is None or bounds is None:
        return None, tuple(issues)
    return ParsedProfile(bodies[CANON], bodies[STYLE], definitions, values, bounds), ()


def render_markdown(profile: ParsedProfile) -> str:
    """Canonical export; ``parse_markdown(render_markdown(p))`` yields the same digest."""

    def block(model: Any) -> str:
        data = json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2)
        return "```json\n" + data + "\n```"

    return (
        "\n\n".join(
            (
                FORMAT_MARKER,
                f"## {CANON}\n\n{profile.canon}",
                f"## {STYLE}\n\n{profile.style}",
                f"## {PROPERTIES}\n\n{block(profile.definitions)}",
                f"## {VALUES}\n\n{block(profile.initial_values)}",
                f"## {BOUNDS}\n\n{block(profile.bounds)}",
            )
        )
        + "\n"
    )
