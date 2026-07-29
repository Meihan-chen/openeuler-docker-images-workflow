"""Normalized, serializable input contract for one image task."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Mapping
from urllib.parse import urlparse


class TaskSpecError(ValueError):
    """Raised when workflow input cannot form a safe task."""


_DOMAINS = {
    "ai": "AI",
    "base": "Base",
    "bigdata": "Bigdata",
    "cloud": "Cloud",
    "database": "Database",
    "distroless": "Distroless",
    "hpc": "HPC",
    "others": "Others",
    "security": "Security",
    "storage": "Storage",
}
_APP_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_OS_VERSION_RE = re.compile(r"^\d{2}\.\d{2}(?:-lts)?(?:-sp\d+)?$")


def _required(raw: Mapping[str, object], field: str) -> str:
    value = str(raw.get(field, "")).strip()
    if not value:
        raise TaskSpecError(f"{field}: value is required")
    return value


def _os_tag(os_version: str) -> str:
    suffix = os_version.replace("-lts-sp", "sp").replace("-lts", "lts")
    return "oe" + suffix.replace(".", "").replace("-", "")


@dataclass(frozen=True)
class TaskSpec:
    app: str
    version: str
    os_version: str
    domain: str
    source_url: str
    scenario: str = "new-image"

    @classmethod
    def from_workflow_dispatch(cls, raw: Mapping[str, object]) -> "TaskSpec":
        app = _required(raw, "app").lower()
        version = _required(raw, "version")
        os_version = _required(raw, "os_version").lower()
        domain_input = _required(raw, "domain")
        source_url = _required(raw, "source_url")

        if not _APP_RE.fullmatch(app):
            raise TaskSpecError("app: use lowercase letters, numbers, dot, dash or underscore")
        if not _VERSION_RE.fullmatch(version):
            raise TaskSpecError("version: contains unsafe characters")
        if not _OS_VERSION_RE.fullmatch(os_version):
            raise TaskSpecError("os_version: unsupported openEuler version format")

        domain = _DOMAINS.get(domain_input.lower())
        if domain is None:
            raise TaskSpecError(f"domain: unsupported category {domain_input!r}")

        parsed_source = urlparse(source_url)
        if parsed_source.scheme != "https" or not parsed_source.netloc:
            raise TaskSpecError("source_url: an absolute HTTPS URL is required")

        return cls(
            app=app,
            version=version,
            os_version=os_version,
            domain=domain,
            source_url=source_url,
        )

    @property
    def task_id(self) -> str:
        return "-".join(
            (
                self.scenario,
                self.domain.lower(),
                self.app,
                self.version,
                self.os_version,
            )
        )

    @property
    def branch(self) -> str:
        return (
            f"auto/{self.scenario}/{self.app}/"
            f"{self.version}-{_os_tag(self.os_version)}"
        )

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, payload: str) -> "TaskSpec":
        raw = json.loads(payload)
        if not isinstance(raw, dict):
            raise TaskSpecError("TaskSpec JSON must contain an object")
        return cls.from_workflow_dispatch(
            {
                "app": raw.get("app"),
                "version": raw.get("version"),
                "os_version": raw.get("os_version"),
                "domain": raw.get("domain"),
                "source_url": raw.get("source_url"),
            }
        )
