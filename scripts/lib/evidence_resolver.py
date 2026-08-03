"""Freeze Creator-provided upstream evidence for independent QA review."""

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
_MAX_EVIDENCE_ENTRIES = 6
_MAX_EXCERPTS_PER_ENTRY = 2
_MAX_CONTEXT_CHARS = 256
_MAX_FIELD_CHARS = {
    "id": 128,
    "claim": 512,
    "source": 1_024,
    "excerpt": 512,
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
    """Extract the repository ref rather than version-looking path text."""
    parsed = urlparse(url)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    host = (parsed.hostname or "").lower()
    if host == "raw.githubusercontent.com":
        return parts[2] if len(parts) >= 3 else None
    if (
        host in {"github.com", "gitcode.com", "gitee.com"}
        and len(parts) >= 4
        and parts[2] in {"tree", "blob", "raw"}
    ):
        return parts[3]
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
    if (
        host in {"gitcode.com", "gitee.com"}
        and len(parts) >= 5
        and parts[2] == "blob"
    ):
        parts[2] = "raw"
        return parsed._replace(
            path="/" + "/".join(parts),
            params="",
            query="",
            fragment="",
        ).geturl()
    return parsed._replace(params="", query="", fragment="").geturl()


def _bundle(
    task: TaskSpec,
    status: str,
    entries: list[dict[str, object]],
    reason: str = "",
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": 2,
        "task_id": task.task_id,
        "scenario": task.scenario,
        "status": status,
        "entries": entries,
    }
    if reason:
        result["reason"] = reason
    return result


def _safe_text(value: object, limit: int) -> str:
    return str(value)[:limit] if value is not None else ""


def _unavailable_entry(item: object, reason: str) -> dict[str, object]:
    source = item if isinstance(item, Mapping) else {}
    excerpts = source.get("excerpts", [])
    return {
        "id": _safe_text(source.get("id", ""), _MAX_FIELD_CHARS["id"]),
        "claim": _safe_text(
            source.get("claim", ""),
            _MAX_FIELD_CHARS["claim"],
        ),
        "source": _safe_text(source.get("source", ""), _MAX_FIELD_CHARS["source"]),
        "excerpts": [
            _safe_text(excerpt, _MAX_FIELD_CHARS["excerpt"])
            for excerpt in excerpts[:_MAX_EXCERPTS_PER_ENTRY]
        ]
        if isinstance(excerpts, list)
        else [],
        "fetch_status": "unavailable",
        "reason": reason,
        "excerpt_checks": [],
    }


def _match_excerpt(content: str, excerpt: str) -> dict[str, object]:
    index = content.find(excerpt)
    if index < 0:
        return {"found": False}
    start = max(0, index - _MAX_CONTEXT_CHARS // 4)
    return {
        "found": True,
        "match_method": "exact",
        "context": content[start : start + _MAX_CONTEXT_CHARS],
    }


def freeze_creator_evidence(
    *,
    task: TaskSpec,
    evidence: object,
    fetcher: EvidenceFetcher = fetch_evidence_source,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Freeze sources and check excerpts without deciding whether claims are true."""
    if not isinstance(evidence, list):
        return _bundle(task, "unavailable", [], "Creator evidence must be a list")
    if not evidence:
        return _bundle(task, "unavailable", [], "Creator provided no evidence")
    if len(evidence) > _MAX_EVIDENCE_ENTRIES:
        return _bundle(
            task,
            "unavailable",
            [
                _unavailable_entry(
                    item,
                    f"Creator evidence accepts at most {_MAX_EVIDENCE_ENTRIES} entries",
                )
                for item in evidence[:_MAX_EVIDENCE_ENTRIES]
            ],
            f"Creator evidence accepts at most {_MAX_EVIDENCE_ENTRIES} entries",
        )

    upstream = _repository_identity(task.source_url)
    upstream_revision = _repository_revision(task.source_url)
    entries: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    source_cache: dict[str, bytes | None] = {}
    started = clock()

    for item in evidence:
        if not isinstance(item, Mapping):
            entries.append(_unavailable_entry(item, "evidence entry must be an object"))
            continue
        evidence_id = str(item.get("id", "")).strip()
        claim = str(item.get("claim", "")).strip()
        source = str(item.get("source", "")).strip()
        excerpts = item.get("excerpts")
        if not evidence_id or not claim or not source:
            entries.append(
                _unavailable_entry(item, "id, claim and source must be non-empty")
            )
            continue
        if evidence_id in seen_ids:
            entries.append(_unavailable_entry(item, "evidence id is duplicated"))
            continue
        seen_ids.add(evidence_id)
        if any(
            len(value) > _MAX_FIELD_CHARS[field]
            for field, value in (("id", evidence_id), ("claim", claim), ("source", source))
        ):
            entries.append(
                _unavailable_entry(item, "evidence field exceeds its size limit")
            )
            continue
        if (
            not isinstance(excerpts, list)
            or not excerpts
            or len(excerpts) > _MAX_EXCERPTS_PER_ENTRY
            or not all(
                isinstance(excerpt, str)
                and excerpt.strip()
                and len(excerpt) <= _MAX_FIELD_CHARS["excerpt"]
                for excerpt in excerpts
            )
        ):
            entries.append(
                _unavailable_entry(
                    item,
                    f"excerpts must contain 1-{_MAX_EXCERPTS_PER_ENTRY} bounded strings",
                )
            )
            continue
        parsed_source = urlparse(source)
        if not _host_supported(parsed_source.hostname):
            entries.append(_unavailable_entry(item, "evidence host is not supported"))
            continue
        if upstream_revision is None:
            entries.append(
                _unavailable_entry(
                    item,
                    "TaskSpec source is not pinned to a revision",
                )
            )
            continue
        if upstream is None or _repository_identity(source) != upstream:
            entries.append(
                _unavailable_entry(item, "source is outside TaskSpec upstream")
            )
            continue
        if _repository_revision(source) != upstream_revision:
            entries.append(
                _unavailable_entry(
                    item,
                    "source is not pinned to TaskSpec revision",
                )
            )
            continue

        canonical_source = _canonical_fetch_url(source)
        if canonical_source not in source_cache:
            remaining = _TOTAL_FETCH_BUDGET_SECONDS - (clock() - started)
            if remaining <= 0:
                source_cache[canonical_source] = None
            else:
                try:
                    raw = fetcher(
                        canonical_source,
                        max_bytes=_MAX_SOURCE_BYTES,
                        timeout=min(_FETCH_TIMEOUT_SECONDS, max(1, int(remaining))),
                    )
                    source_cache[canonical_source] = raw if isinstance(raw, bytes) else None
                except (OSError, ValueError, urllib.error.URLError):
                    source_cache[canonical_source] = None
        raw = source_cache[canonical_source]
        if raw is None:
            entry = _unavailable_entry(item, "source could not be fetched")
            entries.append(entry)
            continue

        content = raw.decode("utf-8", errors="replace")
        entries.append(
            {
                "id": evidence_id,
                "claim": claim,
                "source": source,
                "fetch_status": "available",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "excerpts": list(excerpts),
                "excerpt_checks": [
                    {"index": index, **_match_excerpt(content, excerpt)}
                    for index, excerpt in enumerate(excerpts)
                ],
            }
        )

    all_available = bool(entries) and all(
        entry.get("fetch_status") == "available"
        and all(
            check.get("found") is True
            for check in entry.get("excerpt_checks", [])
            if isinstance(check, Mapping)
        )
        for entry in entries
    )
    return _bundle(task, "available" if all_available else "unavailable", entries)


def _fallback_bundle(
    *,
    task: TaskSpec | None,
    scenario: str,
    evidence: object,
    reason: str,
) -> dict[str, object]:
    entries = (
        [
            _unavailable_entry(item, reason)
            for item in evidence[:_MAX_EVIDENCE_ENTRIES]
        ]
        if isinstance(evidence, list)
        else []
    )
    if task is not None:
        return _bundle(task, "unavailable", entries, reason)
    return {
        "schema_version": 2,
        "scenario": scenario,
        "status": "unavailable",
        "reason": reason,
        "entries": entries,
    }


def resolve_advisory_evidence(
    *,
    task: TaskSpec | None,
    scenario: str,
    evidence: object,
    resolver: Callable[..., Mapping[str, object]] | None,
) -> dict[str, object]:
    """Run the optional resolver without allowing evidence to stop QA."""
    if task is None:
        return _fallback_bundle(
            task=None,
            scenario=scenario,
            evidence=evidence,
            reason="TaskSpec is unavailable; evidence was not fetched",
        )
    if resolver is None:
        return _fallback_bundle(
            task=task,
            scenario=scenario,
            evidence=evidence,
            reason="evidence resolver is not configured",
        )
    try:
        result = resolver(task=task, evidence=evidence)
        if not isinstance(result, Mapping):
            raise TypeError("evidence resolver must return an object")
    except Exception:
        return _fallback_bundle(
            task=task,
            scenario=scenario,
            evidence=evidence,
            reason="evidence resolver failed",
        )
    bundle = dict(result)
    bundle["status"] = (
        "available" if bundle.get("status") == "available" else "unavailable"
    )
    return bundle


def creator_result_for_qa(payload: Mapping[str, object]) -> dict[str, object]:
    """Keep Creator evidence in the single Harness bundle, not twice in the prompt."""
    result = dict(payload)
    if "evidence" in result:
        result["evidence"] = "See Harness-fixed evidence bundle below"
    return result


def render_qa_evidence(bundle: Mapping[str, object]) -> str:
    """Render the shared, non-blocking evidence instructions for either harness."""
    return (
        "\n\n## Harness-fixed Creator evidence bundle\n\n"
        "The Creator supplied the claims, sources, and excerpts. The Harness "
        "only fixed the TaskSpec-pinned source, hash, and exact excerpt match. "
        "Perform the original full review and report evidence judgments in "
        "`evidence_reviews`, separate from `issues`. Missing, unavailable, or "
        "contradictory evidence alone must not create an issue, change status "
        "to `needs_fix`, or trigger Creator repair.\n\n"
        "```json\n"
        + json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n```\n"
    )
