"""Select verified, structured failure patterns for one repair prompt."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

import yaml

_IDENTIFIER_RE = re.compile(r"^\w+$")
_PATTERN_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_DEFAULT_LIMIT = 4
_DEFAULT_MAX_CHARS = 12_000


class FailureKnowledgeError(ValueError):
    """Raised when the curated knowledge file violates its stable schema."""


@dataclass(frozen=True)
class Pattern:
    id: str
    title: str
    stages: tuple[str, ...]
    keywords: tuple[str, ...]
    diagnosis: str
    remediation: tuple[str, ...]
    prevention: str
    verification_sources: tuple[str, ...]

    def matches(self, haystack: str) -> tuple[str, ...]:
        return tuple(
            keyword for keyword in self.keywords if _occurs(keyword, haystack)
        )

    def render(self) -> str:
        fixes = "\n".join(f"- {item}" for item in self.remediation)
        sources = "\n".join(
            f"- {source}" for source in self.verification_sources
        )
        return (
            f"#### {self.id}: {self.title}\n\n"
            f"Stages: {', '.join(self.stages)}\n\n"
            f"Diagnosis: {self.diagnosis}\n\n"
            f"Remediation:\n{fixes}\n\n"
            f"Prevention: {self.prevention}\n\n"
            f"Verification sources:\n{sources}"
        )


def _occurs(keyword: str, haystack: str) -> bool:
    needle = keyword.lower()
    if _IDENTIFIER_RE.fullmatch(needle):
        return re.search(rf"\b{re.escape(needle)}\b", haystack) is not None
    return needle in haystack


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FailureKnowledgeError(f"{field} must be a non-empty string")
    return value.strip()


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise FailureKnowledgeError(f"{field} must be a non-empty list")
    return tuple(_nonempty_string(item, field) for item in value)


def parse_patterns(document: str) -> tuple[Pattern, ...]:
    """Parse only schema-complete patterns backed by verification sources."""
    try:
        raw = yaml.safe_load(document)
    except yaml.YAMLError as error:
        raise FailureKnowledgeError("failure knowledge must be valid YAML") from error
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
        raise FailureKnowledgeError("unsupported failure knowledge schema")
    entries = raw.get("patterns")
    if not isinstance(entries, list) or not entries:
        raise FailureKnowledgeError("failure knowledge must contain patterns")

    patterns: list[Pattern] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(entries):
        field_root = f"patterns[{index}]"
        if not isinstance(entry, Mapping):
            raise FailureKnowledgeError(f"{field_root} must be an object")
        pattern_id = _nonempty_string(entry.get("id"), f"{field_root}.id")
        if not _PATTERN_ID_RE.fullmatch(pattern_id):
            raise FailureKnowledgeError(f"{field_root}.id is invalid")
        if pattern_id in seen_ids:
            raise FailureKnowledgeError(f"duplicate pattern id: {pattern_id}")
        seen_ids.add(pattern_id)
        verification = entry.get("verification")
        if not isinstance(verification, Mapping):
            raise FailureKnowledgeError(
                f"{field_root}.verification must be an object"
            )
        if verification.get("status") != "verified":
            raise FailureKnowledgeError(
                f"{field_root}.verification status must be verified"
            )
        patterns.append(
            Pattern(
                id=pattern_id,
                title=_nonempty_string(
                    entry.get("title"), f"{field_root}.title"
                ),
                stages=_string_list(
                    entry.get("stages"), f"{field_root}.stages"
                ),
                keywords=_string_list(
                    entry.get("symptom_keywords"),
                    f"{field_root}.symptom_keywords",
                ),
                diagnosis=_nonempty_string(
                    entry.get("diagnosis"), f"{field_root}.diagnosis"
                ),
                remediation=_string_list(
                    entry.get("remediation"), f"{field_root}.remediation"
                ),
                prevention=_nonempty_string(
                    entry.get("prevention"), f"{field_root}.prevention"
                ),
                verification_sources=_string_list(
                    verification.get("sources"),
                    f"{field_root}.verification.sources",
                ),
            )
        )
    return tuple(patterns)


def _failure_haystack(evidence: object) -> str:
    chunks: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, str):
            chunks.append(node)
        elif isinstance(node, Mapping):
            for key, value in node.items():
                chunks.append(str(key))
                walk(value)
        elif isinstance(node, Sequence) and not isinstance(node, (bytes, str)):
            for item in node:
                walk(item)
        elif node is not None:
            chunks.append(str(node))

    walk(evidence)
    return "\n".join(chunks).lower()


def select_patterns(
    patterns: Iterable[Pattern],
    evidence: object,
    *,
    limit: int = _DEFAULT_LIMIT,
) -> tuple[Pattern, ...]:
    if limit <= 0:
        return ()
    haystack = _failure_haystack(evidence)
    if not haystack:
        return ()
    scored = [
        (len(hits), -index, pattern)
        for index, pattern in enumerate(patterns)
        if (hits := pattern.matches(haystack))
    ]
    scored.sort(reverse=True)
    return tuple(pattern for _, _, pattern in scored[:limit])


def render_knowledge(
    document: str,
    evidence: object,
    *,
    limit: int = _DEFAULT_LIMIT,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> str:
    patterns = parse_patterns(document)
    selected = select_patterns(patterns, evidence, limit=limit)
    index = "\n".join(
        f"- {pattern.id}: {pattern.title}" for pattern in patterns
    )
    parts = [
        "## Verified failure knowledge",
        "",
        "These are generalized, verified diagnostic aids. Confirm each "
        "symptom against this round's Harness evidence before applying a "
        "remediation; the current failure classification remains authoritative.",
        "",
        "### Index",
        "",
        index,
    ]
    if not selected:
        parts.extend(
            ("", "No verified pattern matched this failure. Diagnose from the evidence alone.")
        )
        return "\n".join(parts) + "\n"
    parts.extend(("", "### Matching patterns", ""))
    budget = max_chars
    for pattern in selected:
        rendered = pattern.render()
        if len(rendered) > budget:
            break
        parts.extend((rendered, ""))
        budget -= len(rendered)
    return "\n".join(parts).rstrip() + "\n"
