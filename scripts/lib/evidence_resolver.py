"""Resolve Agent evidence requests into bounded, reproducible Harness evidence."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from urllib.parse import unquote, urlparse

from scripts.lib.task_spec import TaskSpec

_MAX_SOURCE_BYTES = 262_144
_FETCH_TIMEOUT_SECONDS = 10
_TOTAL_FETCH_BUDGET_SECONDS = 45
_MAX_EVIDENCE_REQUESTS = 12
_MAX_BUNDLE_CHARS = 48_000
_MAX_EXCERPT_CHARS = 2_000
_MAX_FIELD_CHARS = {
    "id": 128,
    "claim": 4_000,
    "source": 2_048,
    "locator": 1_000,
}
_SUPPORTED_EVIDENCE_HOSTS = {
    "github.com",
    "raw.githubusercontent.com",
    "gitcode.com",
    "gitee.com",
}

EvidenceFetcher = Callable[..., bytes]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _host_supported(hostname: str | None) -> bool:
    return bool(hostname and hostname.lower() in _SUPPORTED_EVIDENCE_HOSTS)


def _public_host(hostname: str) -> None:
    """Defense in depth for allowlisted code-hosting DNS responses."""
    addresses = {
        item[4][0]
        for item in socket.getaddrinfo(
            hostname,
            443,
            type=socket.SOCK_STREAM,
        )
    }
    if not addresses:
        raise ValueError("evidence host did not resolve")
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("non-public evidence address is forbidden")


def fetch_evidence_source(
    url: str,
    *,
    max_bytes: int = _MAX_SOURCE_BYTES,
    timeout: int = _FETCH_TIMEOUT_SECONDS,
) -> bytes:
    """Fetch one HTTPS source without credentials, redirects, or unbounded reads."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise ValueError("evidence source must be an absolute credential-free HTTPS URL")
    if not _host_supported(parsed.hostname):
        raise ValueError("evidence host is not supported")
    _public_host(parsed.hostname)
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "openeuler-image-evidence-resolver/1"},
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            content = response.read(max_bytes + 1)
    except (OSError, urllib.error.URLError) as error:
        raise ValueError("evidence source could not be fetched") from error
    if len(content) > max_bytes:
        raise ValueError("evidence source exceeds size limit")
    return content


def _repository_identity(url: str) -> tuple[str, str, str] | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    host = parsed.hostname.lower()
    if host == "raw.githubusercontent.com":
        if len(parts) < 2:
            return None
        return ("github.com", parts[0].lower(), parts[1].removesuffix(".git").lower())
    if len(parts) < 2:
        return None
    return (host, parts[0].lower(), parts[1].removesuffix(".git").lower())


def _repository_revision(url: str) -> str | None:
    """Extract the actual repository ref, never version-looking path text."""
    parsed = urlparse(url)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if parsed.hostname and parsed.hostname.lower() == "raw.githubusercontent.com":
        return parts[2] if len(parts) >= 3 else None
    for marker in ("tree", "blob", "raw"):
        try:
            index = parts.index(marker)
        except ValueError:
            continue
        if index + 1 < len(parts):
            return parts[index + 1]
    return None


def _canonical_fetch_url(url: str) -> str:
    """Fetch repository file content instead of a code-hosting HTML page."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if host == "github.com" and len(parts) >= 5 and parts[2] == "blob":
        return parsed._replace(
            netloc="raw.githubusercontent.com",
            path="/" + "/".join((parts[0], parts[1], *parts[3:])),
            params="",
            query="",
            fragment="",
        ).geturl()
    if host in {"gitcode.com", "gitee.com"} and "blob" in parts:
        index = parts.index("blob")
        parts[index] = "raw"
        return parsed._replace(
            path="/" + "/".join(parts),
            params="",
            query="",
            fragment="",
        ).geturl()
    return parsed._replace(params="", query="", fragment="").geturl()


def _rejected_bundle(task: TaskSpec, reason: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": task.task_id,
        "scenario": task.scenario,
        "status": "rejected",
        "reason": reason,
        "entries": [],
    }


def _request_failure(request: object, reason: str) -> dict[str, object]:
    source = request if isinstance(request, Mapping) else {}
    return {
        "id": str(source.get("id", "")),
        "claim": str(source.get("claim", "")),
        "source": str(source.get("source", "")),
        "locator": str(source.get("locator", "")),
        "resolved": False,
        "reason": reason,
    }


def _excerpt(content: str, locator: str) -> str | None:
    index = content.lower().find(locator.lower())
    if index < 0:
        return None
    before = min(index, _MAX_EXCERPT_CHARS // 4)
    start = index - before
    return content[start : start + _MAX_EXCERPT_CHARS]


def resolve_evidence_requests(
    *,
    task: TaskSpec,
    requests: object,
    fetcher: EvidenceFetcher = fetch_evidence_source,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Resolve evidence only from the TaskSpec upstream and pinned revision."""
    if not isinstance(requests, list):
        return _rejected_bundle(task, "evidence_requests must be a list")
    if not requests:
        return {
            "schema_version": 1,
            "task_id": task.task_id,
            "scenario": task.scenario,
            "status": "not_requested",
            "entries": [],
        }
    if len(requests) > _MAX_EVIDENCE_REQUESTS:
        return _rejected_bundle(
            task,
            f"evidence_requests accepts at most {_MAX_EVIDENCE_REQUESTS} entries",
        )
    for request in requests:
        if not isinstance(request, Mapping):
            continue
        for field, limit in _MAX_FIELD_CHARS.items():
            value = request.get(field)
            if isinstance(value, str) and len(value) > limit:
                return _rejected_bundle(
                    task,
                    f"evidence request {field} exceeds {limit} characters",
                )
    upstream = _repository_identity(task.source_url)
    upstream_revision = _repository_revision(task.source_url)
    entries: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    started = clock()

    for request in requests:
        remaining = _TOTAL_FETCH_BUDGET_SECONDS - (clock() - started)
        if remaining <= 0:
            return _rejected_bundle(
                task,
                "evidence resolution exceeded the total time budget",
            )
        if not isinstance(request, Mapping):
            entries.append(_request_failure(request, "request must be an object"))
            continue
        evidence_id = str(request.get("id", "")).strip()
        claim = str(request.get("claim", "")).strip()
        source = str(request.get("source", "")).strip()
        locator = str(request.get("locator", "")).strip()
        if not evidence_id or not claim or not source or not locator:
            entries.append(_request_failure(request, "request fields must be non-empty"))
            continue
        if evidence_id in seen_ids:
            entries.append(_request_failure(request, "evidence id is duplicated"))
            continue
        seen_ids.add(evidence_id)
        parsed_source = urlparse(source)
        if not _host_supported(parsed_source.hostname):
            entries.append(
                _request_failure(request, "evidence host is not supported")
            )
            continue
        if upstream is None or _repository_identity(source) != upstream:
            entries.append(
                _request_failure(request, "source is outside TaskSpec upstream")
            )
            continue
        source_revision = _repository_revision(source)
        if (
            source_revision is None
            or upstream_revision is None
            or source_revision != upstream_revision
        ):
            entries.append(
                _request_failure(
                    request,
                    "source is not pinned to TaskSpec revision",
                )
            )
            continue
        try:
            canonical_source = _canonical_fetch_url(source)
            raw = fetcher(
                canonical_source,
                max_bytes=_MAX_SOURCE_BYTES,
                timeout=min(
                    _FETCH_TIMEOUT_SECONDS,
                    max(1, int(remaining)),
                ),
            )
        except (OSError, ValueError, urllib.error.URLError):
            entries.append(_request_failure(request, "source could not be fetched"))
            continue
        if not isinstance(raw, bytes):
            entries.append(_request_failure(request, "source fetch returned invalid data"))
            continue
        text = raw.decode("utf-8", errors="replace")
        excerpt = _excerpt(text, locator)
        if excerpt is None:
            entries.append(_request_failure(request, "locator was not found"))
            continue
        entries.append(
            {
                "id": evidence_id,
                "claim": claim,
                "source": source,
                "canonical_source": canonical_source,
                "locator": locator,
                "resolved": True,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "excerpt": excerpt,
            }
        )

    resolved = all(entry["resolved"] is True for entry in entries)
    bundle = {
        "schema_version": 1,
        "task_id": task.task_id,
        "scenario": task.scenario,
        "status": "resolved" if resolved else "unresolved",
        "entries": entries,
    }
    if len(json.dumps(bundle, ensure_ascii=False)) > _MAX_BUNDLE_CHARS:
        return _rejected_bundle(
            task,
            f"resolved evidence bundle exceeds {_MAX_BUNDLE_CHARS} characters",
        )
    return bundle
