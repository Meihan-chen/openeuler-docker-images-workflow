"""Stable request contract for the openEuler image upgrade workflow."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Mapping


class UpgradeContractError(ValueError):
    """Raised when an upgrade invocation cannot form a safe request."""


UPGRADE_DOMAINS = (
    "AI",
    "Bigdata",
    "Cloud",
    "Database",
    "Distroless",
    "HPC",
    "Others",
    "Storage",
)
_DOMAIN_BY_LOWER = {domain.lower(): domain for domain in UPGRADE_DOMAINS}
_OE_RE = re.compile(r"^\d{2}\.\d{2}(?:-lts)?(?:-sp\d+)?$", re.IGNORECASE)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TITLE_RE = re.compile(
    r"^\s*【oe-upgrade】.*?openEuler\s+"
    r"(?P<oe>\d{2}\.\d{2}(?:-lts)?(?:-sp\d+)?)\s*$",
    re.IGNORECASE,
)
_TARGET_LABEL = (
    r"(?:openEuler\s*目标版本\s*[（(]\s*Target openEuler Version\s*[）)]|"
    r"Target openEuler Version)"
)
_SCOPE_LABEL = r"Scope"


def normalize_oe_version(value: object) -> str:
    normalized = str(value).strip().lower()
    if not _OE_RE.fullmatch(normalized):
        raise UpgradeContractError("oe_version: unsupported openEuler version format")
    return normalized


def normalize_scope(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        values = [str(part).strip() for part in value if str(part).strip()]
    else:
        raise UpgradeContractError("scope: expected all or a domain list")
    if not values:
        values = ["all"]
    if len(values) == 1 and values[0].lower() == "all":
        return UPGRADE_DOMAINS
    if any(part.lower() == "all" for part in values):
        raise UpgradeContractError("scope: all cannot be combined with domains")
    unknown = [part for part in values if part.lower() not in _DOMAIN_BY_LOWER]
    if unknown:
        raise UpgradeContractError(f"scope: unsupported domain {unknown[0]!r}")
    selected = {_DOMAIN_BY_LOWER[part.lower()] for part in values}
    return tuple(domain for domain in UPGRADE_DOMAINS if domain in selected)


@dataclass(frozen=True)
class InvocationOptions:
    tracking_issue_number: int
    oe_version: str
    scope: tuple[str, ...]
    mode: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "InvocationOptions":
        try:
            issue_number = int(raw.get("tracking_issue_number", 0))
        except (TypeError, ValueError) as error:
            raise UpgradeContractError(
                "tracking_issue_number: must be a positive integer"
            ) from error
        if issue_number <= 0:
            raise UpgradeContractError(
                "tracking_issue_number: must be a positive integer"
            )
        mode = str(raw.get("mode", "deliver")).strip().lower()
        if mode not in {"plan", "deliver"}:
            raise UpgradeContractError("mode: expected plan or deliver")
        return cls(
            tracking_issue_number=issue_number,
            oe_version=normalize_oe_version(raw.get("oe_version", "")),
            scope=normalize_scope(raw.get("scope", "all")),
            mode=mode,
        )


@dataclass(frozen=True)
class UpgradeRequest:
    schema_version: int
    scenario: str
    tracking_issue_number: int
    oe_version: str
    scope: tuple[str, ...]
    base_sha: str
    request_key: str

    @classmethod
    def create(
        cls,
        *,
        tracking_issue_number: int,
        oe_version: str,
        scope: tuple[str, ...] | list[str],
        base_sha: str,
    ) -> "UpgradeRequest":
        normalized_oe = normalize_oe_version(oe_version)
        normalized_scope = normalize_scope(scope)
        if tracking_issue_number <= 0:
            raise UpgradeContractError(
                "tracking_issue_number: must be a positive integer"
            )
        if not _SHA_RE.fullmatch(base_sha):
            raise UpgradeContractError("base_sha: expected a 40 character commit SHA")
        return cls(
            schema_version=1,
            scenario="oe-upgrade",
            tracking_issue_number=tracking_issue_number,
            oe_version=normalized_oe,
            scope=normalized_scope,
            base_sha=base_sha,
            request_key=request_key(tracking_issue_number, normalized_oe),
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "UpgradeRequest":
        if raw.get("schema_version") != 1 or raw.get("scenario") != "oe-upgrade":
            raise UpgradeContractError("schema_version/scenario: unsupported request")
        try:
            issue_number = int(raw.get("tracking_issue_number", 0))
        except (TypeError, ValueError) as error:
            raise UpgradeContractError("tracking_issue_number: invalid") from error
        request = cls.create(
            tracking_issue_number=issue_number,
            oe_version=str(raw.get("oe_version", "")),
            scope=raw.get("scope", []),  # type: ignore[arg-type]
            base_sha=str(raw.get("base_sha", "")),
        )
        if str(raw.get("request_key", "")) != request.request_key:
            raise UpgradeContractError("request_key: does not match request contents")
        return request

    @classmethod
    def from_json(cls, payload: str) -> "UpgradeRequest":
        raw = json.loads(payload)
        if not isinstance(raw, Mapping):
            raise UpgradeContractError("UpgradeRequest JSON must be an object")
        return cls.from_mapping(raw)

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["scope"] = list(self.scope)
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def request_key(issue_number: int, oe_version: str) -> str:
    material = f"oe-upgrade\0{issue_number}\0{oe_version}"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _field_values(body: str, label: str) -> list[str]:
    pattern = re.compile(
        rf"^\s*\*\*(?:{label})\s*[：:]?\*\*\s*[：:]?\s*(.+?)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    return [match.group(1).strip() for match in pattern.finditer(body)]


def parse_upgrade_issue(
    issue_number: int,
    title: str,
    body: str,
    *,
    mode: str = "deliver",
) -> InvocationOptions:
    title_match = _TITLE_RE.fullmatch(title)
    if not title_match:
        raise UpgradeContractError("title: unsupported oe-upgrade format")
    target_values = _field_values(body, _TARGET_LABEL)
    if len(target_values) > 1:
        raise UpgradeContractError("duplicate Target openEuler Version field")
    if not target_values:
        raise UpgradeContractError("body: Target openEuler Version is required")
    title_oe = normalize_oe_version(title_match.group("oe"))
    body_oe = normalize_oe_version(target_values[0])
    if title_oe != body_oe:
        raise UpgradeContractError("title and body openEuler versions do not match")
    scope_values = _field_values(body, _SCOPE_LABEL)
    if len(scope_values) > 1:
        raise UpgradeContractError("duplicate Scope field")
    return InvocationOptions.from_mapping(
        {
            "tracking_issue_number": issue_number,
            "oe_version": body_oe,
            "scope": scope_values[0] if scope_values else "all",
            "mode": mode,
        }
    )
