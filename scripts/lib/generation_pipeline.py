"""Agent generation stage followed by testcase review and target gates."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence

import yaml

from scripts.lib.agent_runtime import (
    SCRATCH_DIR,
    AgentResult,
    AgentRuntimeError,
    AgentTimeoutError,
    qa_requests_repair,
    run_agent,
    validate_agent_payload,
)
from scripts.lib.evidence_resolver import (
    creator_result_for_qa,
    render_qa_evidence,
    resolve_advisory_evidence,
)
from scripts.lib.failure_classification import classify_failure
from scripts.lib.failure_knowledge import FailureKnowledgeError, render_knowledge
from scripts.lib.progress import log
from scripts.lib.task_spec import TaskSpec
from scripts.lib.target_contract import TargetContractError, validate_generated_target


class GenerationPipelineError(RuntimeError):
    """Raised when an Agent pair or deterministic gate fails closed."""


_PROMPT_DIR = Path(__file__).resolve().parents[2] / ".github" / "agents"
_KNOWLEDGE_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "failure-patterns.yml"
)
_PROMPT_FILES = {
    "image_creator": "image-creator.md",
    "testcase_creator": "testcase-creator.md",
    "testcase_qa": "testcase-qa.md",
    "fixer": "code-fixer.md",
}
_REQUIRED_KEYS = {
    "image_creator": (
        "success",
        "files_created",
        "identity_decision",
    ),
    "testcase_creator": (
        "success",
        "files_created",
        "command_evidence",
    ),
    "testcase_qa": ("status", "issues", "coverage_score", "summary"),
    "fixer": ("success", "changes"),
}
_RUNTIME_REQUIRED_KEYS = {
    "image_creator": ("success", "files_created"),
    "testcase_creator": ("success", "files_created"),
    "testcase_qa": ("issues", "summary"),
    "fixer": ("success", "changes"),
}
# Testcase QA receives a bounded snapshot and Harness-fixed source material.
# Evidence fetching has its own wall-clock budget and completes before this timeout.
_QA_ROLES = {"testcase_qa"}
_QA_TIMEOUT_SECONDS = 1200
_DEFAULT_AGENT_TIMEOUT_SECONDS = 3600
_QA_SNAPSHOT_MAX_CHARS = 64_000
_QA_PROMPT_MAX_CHARS = 100_000
_QA_COMPACT_PREVIEW_CHARS = 4_096
_PHASE1_TASK = (
    "kvrocks",
    "2.16.0",
    "24.03-lts-sp4",
    "Database",
    "https://github.com/apache/kvrocks/tree/v2.16.0",
)
EvidenceResolver = Callable[..., Mapping[str, object]]


@dataclass(frozen=True)
class GenerationResult:
    status: str
    qa_fix_rounds: int
    gate_report: Mapping[str, object]
    qa_disagreements: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class ReviewPairResult:
    fix_rounds: int
    creator_payload: Mapping[str, object] | None
    disagreement: Mapping[str, object] | None = None


def _tag(task: TaskSpec) -> str:
    os_tag = task.os_version.replace(".", "").replace("-lts-sp", "sp")
    return f"{task.version}-oe{os_tag}"


def lint_dockerfile(
    *,
    executable: Path,
    dockerfile: Path,
) -> dict[str, object]:
    try:
        completed = subprocess.run(
            [
                str(executable),
                str(dockerfile),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        return {
            "status": "failed",
            "blocking": True,
            "diagnostic_status": "unavailable",
            "returncode": None,
            "output": str(error),
        }
    output = "\n".join(
        part.strip()
        for part in (completed.stdout, completed.stderr)
        if part.strip()
    )
    return {
        "status": "passed",
        "blocking": False,
        "diagnostic_status": (
            "clean" if completed.returncode == 0 else "findings"
        ),
        "returncode": completed.returncode,
        "output": output,
    }


def _require_phase1_task(task: TaskSpec) -> None:
    """Pin the zero-AI smoke candidate to the one TaskSpec it is written for.

    Generation itself is no longer restricted: the prompts and the target
    gates carry no application knowledge, so the pipeline is free to attempt
    any TaskSpec. This guard exists only because the deterministic smoke
    fixture below intentionally writes one fixed sample candidate.
    """
    actual = (
        task.app,
        task.version,
        task.os_version,
        task.domain,
        task.source_url,
    )
    if actual != _PHASE1_TASK:
        raise GenerationPipelineError(
            "the deterministic smoke candidate only supports the Kvrocks "
            "2.16.0 TaskSpec"
        )


def _candidate_paths(
    *,
    workspace: Path,
    task: TaskSpec,
) -> tuple[Path, ...]:
    app_root = workspace / task.domain / task.app
    image_root = app_root / task.version / task.os_version
    paths = {
        workspace / task.domain / "image-list.yml",
        app_root / "meta.yml",
        app_root / "README.md",
        app_root / "doc" / "image-info.yml",
        app_root / "doc" / "picture" / "logo.png",
        image_root / "Dockerfile",
        app_root / "tests" / "test_helpers.sh",
        app_root / "tests" / "test.sh",
    }
    if app_root.is_dir():
        paths.update(
            path
            for path in app_root.rglob("*")
            if path.is_file()
            and path.relative_to(app_root).parts[0] != "results"
        )
    return tuple(sorted(paths))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_preview(path: Path) -> str:
    half = _QA_COMPACT_PREVIEW_CHARS // 2
    size = path.stat().st_size
    with path.open("rb") as stream:
        head = stream.read(half)
        stream.seek(max(0, size - half))
        tail = stream.read(half)
    return (
        "--- head ---\n"
        + head.decode(errors="replace")
        + "\n--- tail ---\n"
        + tail.decode(errors="replace")
    )


def _bounded_candidate_snapshot(
    *,
    paths: Sequence[Path],
    workspace: Path,
    max_chars: int,
) -> tuple[str, dict[str, object]]:
    """Keep QA input bounded without treating candidate size as a defect."""
    snapshot: dict[str, object] = {}
    manifest: list[dict[str, object]] = []
    compacted: list[str] = []
    hashed_binaries: list[str] = []
    # Reserve room for JSON paths and metadata; spend the rest on complete
    # critical files first, then bounded previews.
    remaining = max(0, max_chars - min(max_chars // 3, len(paths) * 256 + 1024))

    def priority(path: Path) -> tuple[int, str]:
        critical = path.name in {
            "Dockerfile",
            "meta.yml",
            "README.md",
            "test.sh",
        }
        return (0 if critical else 1, str(path))

    for path in sorted(paths, key=priority):
        relative = str(path.relative_to(workspace))
        if not path.is_file():
            snapshot[relative] = "<missing>"
            manifest.append({"path": relative, "status": "missing"})
            continue
        size = path.stat().st_size
        digest = _file_sha256(path)
        with path.open("rb") as stream:
            sample = stream.read(4096)
        is_binary = b"\0" in sample or path.suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".zip",
            ".gz",
            ".xz",
            ".bz2",
            ".7z",
            ".tar",
            ".pdf",
            ".webp",
            ".so",
        }
        if is_binary:
            snapshot[relative] = (
                f"<binary file: {size} bytes, sha256:{digest}>"
            )
            kind = "binary"
            hashed_binaries.append(relative)
        else:
            kind = "text"
            if size <= remaining:
                content = path.read_text(errors="replace")
                if len(content) <= remaining:
                    snapshot[relative] = content
                    remaining -= len(content)
                else:
                    snapshot[relative] = (
                        f"<compacted text file: {size} bytes, "
                        f"sha256:{digest}>\n{_text_preview(path)}"
                    )
                    compacted.append(relative)
            elif remaining >= _QA_COMPACT_PREVIEW_CHARS:
                preview = _text_preview(path)
                snapshot[relative] = (
                    f"<compacted text file: {size} bytes, "
                    f"sha256:{digest}>\n{preview}"
                )
                compacted.append(relative)
                remaining -= len(preview)
            else:
                snapshot[relative] = (
                    f"<compacted text file: {size} bytes, sha256:{digest}; "
                    "preview omitted>"
                )
                compacted.append(relative)
        manifest.append(
            {
                "path": relative,
                "kind": kind,
                "bytes": size,
                "sha256": digest,
            }
        )

    def encode(document: Mapping[str, object]) -> str:
        return json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    manifest_digest = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    metadata: dict[str, object] = {
        "status": "compacted" if compacted else "full",
        "complete_text": not compacted,
        "compacted_files": sorted(compacted),
        "hashed_binary_files": sorted(hashed_binaries),
        "manifest_sha256": manifest_digest,
    }
    document = dict(snapshot)
    if compacted:
        document["__snapshot_metadata__"] = metadata
    encoded = encode(document)
    if len(encoded) <= max_chars:
        return encoded, metadata

    # If even previews do not fit, fall back to a hashed file manifest.
    visible = list(manifest)
    while True:
        fallback = {
            "__snapshot_metadata__": {
                "status": "manifest_only",
                "complete_text": False,
                "file_count": len(manifest),
                "visible_file_count": len(visible),
                "omitted_file_count": len(manifest) - len(visible),
                "manifest_sha256": manifest_digest,
            },
            "files": visible,
        }
        encoded = encode(fallback)
        if len(encoded) <= max_chars or not visible:
            return encoded, dict(fallback["__snapshot_metadata__"])
        visible.pop()


def _failure_knowledge_section(review: Mapping[str, object]) -> str:
    """Inline the failure patterns whose symptoms match this evidence.

    The knowledge base is advisory, so an unreadable or missing file degrades
    to no section rather than failing the repair round.
    """
    try:
        document = _KNOWLEDGE_PATH.read_text()
    except (OSError, UnicodeError):
        return ""
    try:
        return render_knowledge(document, review)
    except FailureKnowledgeError:
        return ""


def build_role_prompt(
    *,
    role: str,
    task: TaskSpec,
    base_sha: str,
    review: Mapping[str, object] | None = None,
    workspace: Path | None = None,
) -> str:
    try:
        instructions = (_PROMPT_DIR / _PROMPT_FILES[role]).read_text()
    except (KeyError, OSError) as error:
        raise GenerationPipelineError(f"prompt is unavailable for role {role}") from error
    app_root = f"{task.domain}/{task.app}"
    image_root = f"{app_root}/{task.version}/{task.os_version}"
    contract_lines = [
        "## Immutable task contract",
        "",
        f"- Target base SHA: `{base_sha}`",
        f"- TaskSpec: `{task.to_json()}`",
        f"- New MDU root: `{app_root}/`",
        f"- Dockerfile: `{image_root}/Dockerfile`",
        f"- Future result root: `{app_root}/results/{task.version}/{task.os_version}/`",
        f"- Meta tag: `{_tag(task)}`",
        f"- Pinned source URL: `{task.source_url}`",
        f"- Existing list allowed to change: `{task.domain}/image-list.yml`",
        "- Derive application-specific build and runtime behavior from the "
        "official upstream and the candidate files; do not assume a fixed "
        "user, port, health command, build command or persistence path.",
        "- Do not create workflow control assets, lifecycle mode files, or "
        "target-side readiness scripts.",
        "- Do not modify any other path.",
    ]
    if role in {"testcase_creator", "testcase_qa", "fixer"}:
        contract_lines.extend(
            (
                "- Derive application-specific tests from the Dockerfile and "
                "official upstream behavior; do not copy another "
                "application's commands, ports or user assertions.",
                "- The native `runtime_test` harness automatically chooses "
                "the test container from image runtime events and executes "
                "the shared test.sh exactly once. Test scripts must not "
                "invoke Docker, manage the service lifecycle, or implement "
                "a generic readiness loop.",
                "- A service test must exercise a real protocol or data path; "
                "a CLI or batch test must exercise a real command and verify "
                "its output. Process, port, version, or file existence alone "
                "is not a complete functional test.",
                "- Identity, port, path, binary and command expectations must "
                "match the final Dockerfile, not an earlier candidate.",
                f"- Shared tests: `{app_root}/tests/`",
                f"- Shared test entrypoint: `{app_root}/tests/test.sh`",
            )
        )
    if role in {"testcase_creator", "testcase_qa"}:
        contract_lines.append(
            "- Only the shared test paths above are writable; image-list, "
            "Dockerfile, metadata, "
            "documentation and logo are read-only."
        )
    if role == "fixer":
        if workspace is None:
            fixer_whitelist = (
                f"{task.domain}/image-list.yml",
                f"{app_root}/meta.yml",
                f"{app_root}/README.md",
                f"{app_root}/doc/image-info.yml",
                f"{app_root}/doc/picture/logo.png",
                f"{image_root}/Dockerfile",
                f"{app_root}/tests/test_helpers.sh",
                f"{app_root}/tests/test.sh",
            )
        else:
            workspace = Path(workspace)
            fixer_whitelist = tuple(
                path.relative_to(workspace).as_posix()
                for path in _candidate_paths(
                    workspace=workspace,
                    task=task,
                )
            )
        contract_lines.extend(
            (
                "## Fixer whitelist (only these files may be modified)",
                *(f"- `{path}`" for path in fixer_whitelist),
                "- If a fix changes the observable runtime contract, re-read "
                "and synchronize all dependent candidate files without "
                "weakening tests.",
            )
        )
    if role == "image_creator":
        contract_lines.extend(
            (
                "- Docker may only be used for bounded, read-only inspection "
                "of the TaskSpec base image.",
                "- Write the minimum complete candidate before optional "
                "research; leave uncertain facts to `native_build` and "
                "`runtime_test` through `assumptions`.",
            )
        )
    contract_lines.extend(
        (
            "- Do not install or upgrade host tools or packages with brew, "
            "apt, dnf, yum, pip, or similar commands.",
            "- Do not compile or build the target application, whether "
            "directly on the Runner or inside `docker run`. Do not invoke "
            "linters; the harness runs those validations after your response.",
            f"- Put all downloads, archives and temporary files under "
            f"`{SCRATCH_DIR}/` (also in `$OE_AGENT_SCRATCH`). Never unpack or "
            "write scratch content anywhere else in this repository.",
        )
    )
    contract_lines.append(
        "- Do not run git commit, git push, or any GitCode API write."
    )
    context = "\n".join(contract_lines)
    parts = [instructions.rstrip(), context]
    if review is not None:
        parts.extend(
            (
                "## Review report to resolve",
                "",
                "Only fix the reported issues; do not regenerate unrelated content.",
                "",
                "```json",
                json.dumps(review, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
            )
        )
        if role == "fixer":
            knowledge = _failure_knowledge_section(review)
            if knowledge:
                parts.append(knowledge.rstrip("\n"))
    quoted_keys = [f"`{key}`" for key in _REQUIRED_KEYS[role]]
    if len(quoted_keys) == 2:
        output_keys = " and ".join(quoted_keys)
    else:
        output_keys = ", ".join(quoted_keys[:-1]) + f", and {quoted_keys[-1]}"
    parts.extend(
        (
            "## Final response contract",
            "",
            "Do not use a shell command, `echo`, or another tool to print the "
            "final JSON; tool output is not the final response.",
            "Your final response MUST be exactly one JSON object containing "
            f"the documented {output_keys} keys. Do not end with prose, "
            "Markdown, a table, or commentary.",
        )
    )
    return "\n\n".join(parts) + "\n"


def _qa_prompt(
    *,
    role: str,
    workspace: Path,
    task: TaskSpec,
    base_sha: str,
    previous_review: Mapping[str, object] | None = None,
    creator_payload: Mapping[str, object] | None = None,
    evidence_bundle: Mapping[str, object] | None = None,
    return_snapshot: bool = False,
) -> str | tuple[str, Mapping[str, object]]:
    app_root = workspace / task.domain / task.app
    image_root = app_root / task.version / task.os_version
    if role != "testcase_qa":
        raise GenerationPipelineError(f"unsupported QA role: {role}")
    tests_root = app_root / "tests"
    paths = [
        image_root / "Dockerfile",
        tests_root / "test_helpers.sh",
        tests_root / "test.sh",
    ]
    if tests_root.is_dir():
        paths.extend(
            sorted(
                path
                for path in tests_root.rglob("*")
                if path.is_file() and path not in paths
            )
        )

    previous_findings = ""
    if previous_review is not None:
        previous_findings = (
            "\n## Previous QA findings to verify\n\n"
            "This is a new independent QA session. Verify every previous "
            "finding against the latest candidate, then perform a complete "
            "review for new issues.\n\n"
            "```json\n"
            + json.dumps(
                {"issues": previous_review.get("issues", [])},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n```\n"
        )
    creator_evidence = ""
    if isinstance(creator_payload, Mapping):
        entries = creator_payload.get("command_evidence")
        if isinstance(entries, list):
            creator_evidence = (
                "\n## Testcase Creator command evidence\n\n"
                "The JSON below is the latest complete Creator result. The "
                "Creator claims these semantics for the application "
                "commands the tests rely on. Compare each claim with the "
                "Harness-fixed source bundle below; do not treat the Creator's "
                "citation alone as verification. Record an actual candidate "
                "issue only when the test files themselves are defective. "
                "Missing command evidence belongs only in `evidence_reviews`.\n\n"
                "```json\n"
                + json.dumps(
                    creator_result_for_qa(creator_payload),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n```\n"
            )
    harness_evidence = ""
    if isinstance(evidence_bundle, Mapping):
        harness_evidence = render_qa_evidence(evidence_bundle)
    prompt_prefix = (
        build_role_prompt(role=role, task=task, base_sha=base_sha)
        + creator_evidence
        + harness_evidence
        + previous_findings
        + "\n## Embedded candidate snapshot\n\n"
        + "Review only the file snapshot below. Do not call tools or read the "
        "workspace; return the documented JSON review contract directly.\n\n"
        + "Snapshot metadata is Harness-owned. If complete_text is false, "
        "review only visible content and state the limitation in the summary.\n\n"
        + "```json\n"
    )
    prompt_suffix = "\n```\n"
    available = _QA_PROMPT_MAX_CHARS - len(prompt_prefix) - len(prompt_suffix)
    if available <= 0:
        raise GenerationPipelineError(
            "QA non-snapshot context is too large for the bounded review"
        )
    encoded_snapshot, snapshot_metadata = _bounded_candidate_snapshot(
        paths=paths,
        workspace=workspace,
        max_chars=min(_QA_SNAPSHOT_MAX_CHARS, available),
    )
    if snapshot_metadata.get("status") != "full":
        log("review", f"SNAPSHOT status={snapshot_metadata.get('status')}")
    prompt = prompt_prefix + encoded_snapshot + prompt_suffix
    if len(prompt) > _QA_PROMPT_MAX_CHARS:
        raise GenerationPipelineError(
            "QA prompt is too large for the bounded review"
        )
    if return_snapshot:
        return prompt, snapshot_metadata
    return prompt


def _redact(value: object, secret: str) -> object:
    if isinstance(value, str):
        return value.replace(secret, "REDACTED")
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, secret) for key, item in value.items()}
    return value


def _write_report(
    report_dir: Path,
    name: str,
    payload: Mapping[str, object],
    api_key: str,
) -> None:
    safe_payload = _redact(dict(payload), api_key)
    (report_dir / name).write_text(
        json.dumps(safe_payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )


def _log_review_result(
    *,
    qa_role: str,
    round_number: int,
    payload: Mapping[str, object],
    api_key: str,
) -> None:
    issues = payload.get("issues", [])
    issue_count = len(issues) if isinstance(issues, list) else 0
    summary = " ".join(str(payload.get("summary", "")).split())
    if api_key:
        summary = summary.replace(api_key, "REDACTED")
    summary = summary[:200]
    log(
        "review",
        f"RESULT {qa_role} round={round_number} "
        f"status={payload.get('status')} issues={issue_count} "
        f"summary={json.dumps(summary, ensure_ascii=False)}",
    )


def _normalize_qa_payload(
    payload: Mapping[str, object],
    *,
    require_coverage: bool,
    snapshot: Mapping[str, object],
    issue_root: str | None = None,
) -> dict[str, object]:
    """Map QA outcome mistakes onto existing orchestration without an Agent call."""
    normalized = dict(payload)
    for reserved in (
        "reported_status",
        "reported_coverage_score",
        "protocol_warnings",
        "harness",
    ):
        normalized.pop(reserved, None)
    harness: dict[str, object] = {"snapshot": dict(snapshot)}
    warnings: list[dict[str, object]] = []
    raw_issues = normalized.get("issues")

    def actionable(issue: object) -> bool:
        if not isinstance(issue, Mapping):
            return False
        description = issue.get("description")
        if not isinstance(description, str) or not description.strip():
            return False
        if issue_root is None:
            return True
        file = issue.get("file")
        evidence = issue.get("evidence")
        if not isinstance(file, str) or not file.strip() or not evidence:
            return False
        path = PurePosixPath(file.replace("\\", "/"))
        root = PurePosixPath(issue_root)
        return (
            not path.is_absolute()
            and ".." not in path.parts
            and path.parts[: len(root.parts)] == root.parts
            and len(path.parts) > len(root.parts)
        )

    actionable_issues = (
        [dict(issue) for issue in raw_issues if actionable(issue)]
        if isinstance(raw_issues, list)
        else []
    )
    normalized["issues"] = actionable_issues
    if isinstance(raw_issues, list) and len(actionable_issues) != len(raw_issues):
        warnings.append(
            {
                "field": "issues",
                "reported": len(raw_issues),
                "effective": len(actionable_issues),
                "message": (
                    "Out-of-scope or malformed QA issues were ignored; only "
                    "actionable test-file issues can trigger Creator repair."
                ),
            }
        )

    reported_status = normalized.get("status")
    effective_status = (
        "unavailable"
        if normalized.get("harness_qa_timeout") is True
        else "needs_fix" if actionable_issues else "approved"
    )
    normalized["status"] = effective_status
    if reported_status != effective_status:
        harness["reported_status"] = reported_status
        warnings.append(
            {
                "field": "status",
                "reported": reported_status,
                "effective": effective_status,
                "message": (
                    "QA status disagreed with the actionable issue list; "
                    "the Harness derived the outcome from actual issues."
                ),
            }
        )

    score = normalized.get("coverage_score")
    if require_coverage and (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not 0 <= score <= 1
    ):
        normalized["coverage_score"] = None
        harness["reported_coverage_score"] = score
        warnings.append(
            {
                "field": "coverage_score",
                "reported": score,
                "effective": None,
                "message": (
                    "Invalid QA coverage score was recorded as unavailable."
                ),
            }
        )

    if warnings:
        harness["protocol_warnings"] = warnings
    normalized["harness"] = harness
    return normalized


def _normalize_and_log_qa_result(
    *,
    qa_role: str,
    round_number: int,
    payload: Mapping[str, object],
    api_key: str,
    snapshot: Mapping[str, object],
    issue_root: str | None = None,
) -> Mapping[str, object]:
    normalized = _normalize_qa_payload(
        payload,
        require_coverage=qa_role == "testcase_qa",
        snapshot=snapshot,
        issue_root=issue_root,
    )
    harness = normalized.get("harness", {})
    warnings = (
        harness.get("protocol_warnings", [])
        if isinstance(harness, Mapping)
        else []
    )
    if isinstance(warnings, list):
        for warning in warnings:
            if not isinstance(warning, Mapping):
                continue
            reported = _redact(warning.get("reported"), api_key)
            effective = _redact(warning.get("effective"), api_key)
            effective_text = (
                "null" if effective is None else str(effective)
            )
            log(
                "review",
                f"WARNING {qa_role} round={round_number} "
                f"field={warning.get('field')} "
                f"reported={json.dumps(reported, ensure_ascii=False)} "
                f"effective={effective_text}",
            )
    return normalized


def _default_validator(
    *,
    workspace: Path,
    task: TaskSpec,
    base_sha: str,
    phase: str,
) -> dict[str, object]:
    return validate_generated_target(
        repo=workspace,
        task=task,
        base_sha=base_sha,
        phase=phase,
    )


def _target_gate_report(
    *,
    target_validator: Callable[..., Mapping[str, object]],
    workspace: Path,
    task: TaskSpec,
    base_sha: str,
    phase: str,
) -> Mapping[str, object]:
    try:
        return target_validator(
            workspace=workspace,
            task=task,
            base_sha=base_sha,
            phase=phase,
        )
    except (TargetContractError, OSError, UnicodeError) as error:
        report: dict[str, object] = {
            "status": "failed",
            "build_allowed": False,
            "delivery_allowed": False,
            "test_allowed": False,
            "errors": str(error).splitlines(),
        }
        if isinstance(error, TargetContractError) and error.findings:
            report["findings"] = error.findings
        return report


def _run(
    *,
    agent_runner: Callable[..., AgentResult],
    executable: Path,
    workspace: Path,
    api_key: str,
    role: str,
    prompt: str,
    report_dir: Path,
) -> AgentResult:
    timeout = (
        _QA_TIMEOUT_SECONDS
        if role in _QA_ROLES
        else _DEFAULT_AGENT_TIMEOUT_SECONDS
    )
    try:
        return agent_runner(
            executable=executable,
            role=role,
            prompt=prompt,
            workspace=workspace,
            api_key=api_key,
            required_keys=_RUNTIME_REQUIRED_KEYS[role],
            timeout=timeout,
        )
    except AgentTimeoutError as error:
        if role in _QA_ROLES:
            # QA never held a veto, so an unfinished review is an advisory gap
            # rather than a reason to discard a candidate the gates can judge.
            log("review", f"QA_TIMEOUT {role} elapsed={error.elapsed:.1f}s")
            _write_report(
                report_dir,
                f"{role.replace('_', '-')}-timeout.json",
                {
                    "status": "timeout",
                    "stage": "agent",
                    "role": role,
                    "elapsed": error.elapsed,
                },
                api_key,
            )
            return AgentResult(
                role=role,
                payload={
                    "status": "unavailable",
                    "issues": [],
                    "summary": (
                        "QA did not finish within its budget; recorded as "
                        "advisory and left to the deterministic gates."
                    ),
                    "harness_qa_timeout": True,
                },
            )
        raise _agent_failure(
            report_dir=report_dir,
            role=role,
            error=error,
            api_key=api_key,
        ) from error
    except AgentRuntimeError as error:
        raise _agent_failure(
            report_dir=report_dir,
            role=role,
            error=error,
            api_key=api_key,
        ) from error


def _agent_failure(
    *,
    report_dir: Path,
    role: str,
    error: Exception,
    api_key: str,
) -> GenerationPipelineError:
    _write_report(
        report_dir,
        "generation-failure.json",
        {
            "status": "failed",
            "stage": "agent",
            "role": role,
            "error": str(error),
        },
        api_key,
    )
    return GenerationPipelineError(f"{role} Agent failed: {error}")


def _review_pair(
    *,
    creator_role: str,
    qa_role: str,
    agent_runner: Callable[..., AgentResult],
    executable: Path,
    workspace: Path,
    report_dir: Path,
    task: TaskSpec,
    base_sha: str,
    api_key: str,
    post_repair_check: (
        Callable[[Mapping[str, object]], None] | None
    ) = None,
    creator_payload: Mapping[str, object] | None = None,
    resolve_evidence: (
        Callable[
            [Mapping[str, object], int],
            Mapping[str, object] | None,
        ]
        | None
    ) = None,
) -> ReviewPairResult:
    def qa_prompt(
        round_number: int,
        previous_review: Mapping[str, object] | None = None,
        creator: Mapping[str, object] | None = creator_payload,
    ) -> tuple[str, Mapping[str, object]]:
        try:
            evidence = (
                resolve_evidence(creator, round_number)
                if resolve_evidence is not None and isinstance(creator, Mapping)
                else None
            )
            result = _qa_prompt(
                role=qa_role,
                workspace=workspace,
                task=task,
                base_sha=base_sha,
                previous_review=previous_review,
                creator_payload=creator,
                evidence_bundle=evidence,
                return_snapshot=True,
            )
            if not isinstance(result, tuple):
                raise GenerationPipelineError("QA snapshot metadata is missing")
            return result
        except GenerationPipelineError as error:
            _write_report(
                report_dir,
                "generation-failure.json",
                {
                    "status": "failed",
                    "stage": "qa_snapshot",
                    "role": qa_role,
                    "error": str(error),
                },
                api_key,
            )
            raise

    log("review", f"START {qa_role} round=1")
    first_prompt, first_snapshot = qa_prompt(1)
    review = _run(
        agent_runner=agent_runner,
        executable=executable,
        workspace=workspace,
        api_key=api_key,
        role=qa_role,
        report_dir=report_dir,
        prompt=first_prompt,
    )
    review_payload = _normalize_and_log_qa_result(
        qa_role=qa_role,
        round_number=1,
        payload=review.payload,
        api_key=api_key,
        snapshot=first_snapshot,
        issue_root=f"{task.domain}/{task.app}/tests",
    )
    _write_report(report_dir, f"{qa_role.replace('_', '-')}-round1.json", review_payload, api_key)
    _log_review_result(
        qa_role=qa_role,
        round_number=1,
        payload=review_payload,
        api_key=api_key,
    )
    if review_payload.get("status") == "unavailable":
        log("review", f"ADVISORY {qa_role} round=1 unavailable")
        return ReviewPairResult(
            fix_rounds=0,
            creator_payload=creator_payload,
            disagreement={
                "role": qa_role,
                "round": 1,
                "status": "unavailable",
                "issues": [],
                "summary": str(review_payload.get("summary", "")),
            },
        )
    if review_payload.get("status") == "approved":
        log("review", f"PASS {qa_role} round=1")
        return ReviewPairResult(
            fix_rounds=0,
            creator_payload=creator_payload,
        )
    if not qa_requests_repair(review_payload):
        log(
            "review",
            f"ADVISORY {qa_role} round=1 evidence-only needs_fix ignored",
        )
        return ReviewPairResult(
            fix_rounds=0,
            creator_payload=creator_payload,
        )

    log("review", f"NEEDS_FIX {qa_role} round=1")
    log("repair", f"START {creator_role} round=2")
    fixed = _run(
        agent_runner=agent_runner,
        executable=executable,
        workspace=workspace,
        api_key=api_key,
        role=creator_role,
        report_dir=report_dir,
        prompt=build_role_prompt(
            role=creator_role,
            task=task,
            base_sha=base_sha,
            review=review_payload,
        ),
    )
    _write_report(
        report_dir,
        f"{creator_role.replace('_', '-')}-round2.json",
        fixed.payload,
        api_key,
    )
    if fixed.payload.get("success") is not True:
        raise GenerationPipelineError(f"{creator_role} repair failed")
    log("repair", f"PASS {creator_role} round=2")

    if post_repair_check is not None:
        post_repair_check(fixed.payload)

    log("review", f"START {qa_role} round=2")
    second_prompt, second_snapshot = qa_prompt(
        2,
        review_payload,
        fixed.payload,
    )
    second = _run(
        agent_runner=agent_runner,
        executable=executable,
        workspace=workspace,
        api_key=api_key,
        role=qa_role,
        report_dir=report_dir,
        prompt=second_prompt,
    )
    second_payload = _normalize_and_log_qa_result(
        qa_role=qa_role,
        round_number=2,
        payload=second.payload,
        api_key=api_key,
        snapshot=second_snapshot,
        issue_root=f"{task.domain}/{task.app}/tests",
    )
    _write_report(
        report_dir,
        f"{qa_role.replace('_', '-')}-round2.json",
        second_payload,
        api_key,
    )
    _log_review_result(
        qa_role=qa_role,
        round_number=2,
        payload=second_payload,
        api_key=api_key,
    )
    second_status = second_payload.get("status")
    if second_status == "unavailable":
        log("review", f"ADVISORY {qa_role} round=2 unavailable")
        disagreement = {
            "role": qa_role,
            "round": 2,
            "status": "unavailable",
            "issues": [],
            "summary": str(second_payload.get("summary", "")),
        }
    elif second_status == "approved":
        log("review", f"PASS {qa_role} round=2")
        disagreement = None
    elif second_status == "needs_fix" and qa_requests_repair(second_payload):
        log(
            "review",
            f"DISAGREEMENT {qa_role} round=2; continue=local_validation",
        )
        raw_issues = second_payload.get("issues", [])
        disagreement = {
            "role": qa_role,
            "round": 2,
            "issues": raw_issues if isinstance(raw_issues, list) else [],
            "summary": str(second_payload.get("summary", "")),
        }
    elif second_status == "needs_fix":
        log(
            "review",
            f"ADVISORY {qa_role} round=2 evidence-only needs_fix ignored",
        )
        disagreement = None
    return ReviewPairResult(
        fix_rounds=1,
        creator_payload=fixed.payload,
        disagreement=disagreement,
    )


def run_generation_pipeline(
    *,
    workspace: Path,
    report_dir: Path,
    task: TaskSpec,
    base_sha: str,
    executable: Path,
    api_key: str,
    agent_runner: Callable[..., AgentResult] = run_agent,
    target_validator: Callable[..., Mapping[str, object]] = _default_validator,
    image_linter: (
        Callable[[Path], Mapping[str, object]] | None
    ) = None,
    evidence_resolver: EvidenceResolver | None = None,
) -> GenerationResult:
    workspace = Path(workspace).resolve()
    report_dir = Path(report_dir).resolve()
    if report_dir == workspace or workspace in report_dir.parents:
        raise GenerationPipelineError(
            "Agent evidence directory must remain outside the target workspace"
        )
    if report_dir.exists() and any(report_dir.iterdir()):
        raise GenerationPipelineError("Agent evidence directory must be empty")
    report_dir.mkdir(parents=True, exist_ok=True)
    dockerfile = (
        workspace
        / task.domain
        / task.app
        / task.version
        / task.os_version
        / "Dockerfile"
    )
    app_root = workspace / task.domain / task.app
    tests_root = app_root / "tests"

    def resolve_creator_evidence(
        role: str,
        payload: Mapping[str, object],
        round_number: int,
    ) -> Mapping[str, object] | None:
        evidence = payload.get("evidence", [])
        bundle = resolve_advisory_evidence(
            task=task,
            scenario=task.scenario,
            evidence=evidence,
            resolver=evidence_resolver,
        )
        _write_report(
            report_dir,
            (
                f"{role.replace('_', '-')}-round{round_number}-"
                "evidence-bundle.json"
            ),
            bundle,
            api_key,
        )
        return bundle

    def merge_creator_contracts(
        report: Mapping[str, object],
        creator_payloads: Mapping[str, Mapping[str, object]],
    ) -> Mapping[str, object]:
        merged = dict(report)
        findings = list(merged.get("findings", []))
        errors = list(merged.get("errors", []))
        initial_finding_count = len(findings)
        testcase_contract_failed = False
        for owner, payload in creator_payloads.items():
            if owner == "image_creator":
                contract_key = "identity_decision"
                code = "agent.identity_decision"
            else:
                contract_key = "command_evidence"
                code = "agent.command_evidence"
            try:
                validate_agent_payload(
                    payload,
                    required_keys=(contract_key,),
                )
            except AgentRuntimeError as error:
                if owner == "testcase_creator":
                    testcase_contract_failed = True
                message = str(error)
                findings.append(
                    {
                        "code": code,
                        "level": "delivery_stop",
                        "owner": owner,
                        "source": "agent_output_contract",
                        "message": message,
                    }
                )
                errors.append(message)
        if len(findings) != initial_finding_count:
            merged["delivery_allowed"] = False
            if testcase_contract_failed:
                merged["test_allowed"] = False
            merged["findings"] = findings
            merged["errors"] = errors
        return merged

    def enforce_gate(
        *,
        phase: str,
        report_name: str,
        stage: str,
        failure_message: str,
        creator_payloads: (
            Mapping[str, Mapping[str, object]] | None
        ) = None,
    ) -> Mapping[str, object]:
        log("gate", f"START {stage}")
        report = _target_gate_report(
            target_validator=target_validator,
            workspace=workspace,
            task=task,
            base_sha=base_sha,
            phase=phase,
        )
        if creator_payloads:
            report = merge_creator_contracts(report, creator_payloads)
        _write_report(report_dir, report_name, report, api_key)
        build_allowed = report.get(
            "build_allowed",
            report.get("status") == "passed",
        )
        if build_allowed is not True:
            raise GenerationPipelineError(failure_message)
        if report.get("delivery_allowed", True) is not True:
            finding_count = len(report.get("findings", []))
            log(
                "gate",
                f"NEEDS_FIX {stage} delivery_findings={finding_count}",
            )
            return report
        log("gate", f"PASS {stage}")
        return report

    def lint_image(
        *,
        report_name: str,
        stage: str,
    ) -> Mapping[str, object]:
        if image_linter is None:
            return {"status": "passed"}
        log("lint", f"START {stage}")
        report = image_linter(dockerfile)
        _write_report(report_dir, report_name, report, api_key)
        detail = " ".join(
            str(_redact(report.get("output", ""), api_key)).split()
        )[:500]
        detail = detail or "no lint output"
        if report.get("blocking") is True or report.get("status") != "passed":
            raise GenerationPipelineError(
                f"{stage} could not run: {detail}"
            )
        if report.get("diagnostic_status") == "findings":
            log("lint", f"ADVISORY {stage}: {detail}")
            return report
        log("lint", f"PASS {stage}")
        return report

    def owner_gate_findings(
        report: Mapping[str, object],
        *,
        owner: str,
    ) -> Mapping[str, object] | None:
        raw_findings = report.get("findings", [])
        if not isinstance(raw_findings, list):
            return None
        findings = [
            finding
            for finding in raw_findings
            if isinstance(finding, Mapping)
            and finding.get("owner") == owner
            and finding.get("level") == "delivery_stop"
        ]
        if not findings:
            return None
        return {
            **report,
            "findings": findings,
            "errors": [str(finding.get("message", "")) for finding in findings],
        }

    def image_owned_snapshot() -> dict[str, str]:
        return {
            str(path.relative_to(workspace)): (
                f"{path.stat().st_mode & 0o777:o}:"
                f"{_file_sha256(path)}"
            )
            for path in _candidate_paths(
                workspace=workspace,
                task=task,
            )
            if path.is_file() and tests_root not in path.parents
        }

    def repair_deterministic_failure(
        *,
        creator_role: str,
        report_name: str,
        findings: Mapping[str, object],
    ) -> Mapping[str, object]:
        gate = findings.get("gate")
        lint = findings.get("lint")
        classification = classify_failure(
            gate=gate if isinstance(gate, Mapping) else None,
            lint=lint if isinstance(lint, Mapping) else None,
            allowed_roots=(task.domain,),
        )
        validation_report = {
            "status": "needs_fix",
            "classification": classification,
            "issues": [
                {
                    "severity": "blocker",
                    "description": "Deterministic validation failed.",
                    "findings": findings,
                }
            ],
            "summary": (
                "Fix only the deterministic validation findings, "
                "then return the required Creator result. "
                + str(classification["guidance"])
            ),
        }
        log("repair", f"START {creator_role} deterministic_validation")
        repaired = _run(
            agent_runner=agent_runner,
            executable=executable,
            workspace=workspace,
            api_key=api_key,
            role=creator_role,
            report_dir=report_dir,
            prompt=build_role_prompt(
                role=creator_role,
                task=task,
                base_sha=base_sha,
                review=validation_report,
            ),
        )
        _write_report(
            report_dir,
            report_name,
            repaired.payload,
            api_key,
        )
        if repaired.payload.get("success") is not True:
            raise GenerationPipelineError(
                f"{creator_role} deterministic repair failed"
            )
        log("repair", f"PASS {creator_role} deterministic_validation")
        return repaired.payload

    log("generate", "START image_creator")
    creator = _run(
        agent_runner=agent_runner,
        executable=executable,
        workspace=workspace,
        api_key=api_key,
        role="image_creator",
        report_dir=report_dir,
        prompt=build_role_prompt(
            role="image_creator",
            task=task,
            base_sha=base_sha,
        ),
    )
    _write_report(report_dir, "image-creator.json", creator.payload, api_key)
    if creator.payload.get("success") is not True:
        raise GenerationPipelineError("image_creator did not complete successfully")
    log("generate", "PASS image_creator")
    latest_image_payload: Mapping[str, object] = creator.payload

    image_gate = enforce_gate(
        phase="image",
        report_name="image-precheck-gates.json",
        stage="image_precheck",
        failure_message="deterministic image precheck did not pass",
        creator_payloads={"image_creator": latest_image_payload},
    )
    lint_image(
        report_name="image-lint.json",
        stage="image_lint",
    )
    image_findings = owner_gate_findings(
        image_gate,
        owner="image_creator",
    )
    if image_findings is not None:
        latest_image_payload = repair_deterministic_failure(
            creator_role="image_creator",
            report_name="image-creator-precheck-repair.json",
            findings={"gate": image_findings},
        )
        enforce_gate(
            phase="image",
            report_name="image-precheck-repair-gates.json",
            stage="image_precheck_repair",
            failure_message=(
                "deterministic image repair precheck did not pass"
            ),
            creator_payloads={"image_creator": latest_image_payload},
        )
        lint_image(
            report_name="image-precheck-repair-lint.json",
            stage="image_lint_repair",
        )

    fix_rounds = 0
    disagreements: list[Mapping[str, object]] = []
    frozen_image = image_owned_snapshot()

    def enforce_testcase_ownership(
        *,
        report_name: str,
        role: str,
    ) -> None:
        current = image_owned_snapshot()
        changed = sorted(
            relative
            for relative in set(frozen_image) | set(current)
            if frozen_image.get(relative) != current.get(relative)
        )
        report: dict[str, object] = {
            "status": "failed" if changed else "passed",
            "changed_files": changed,
        }
        _write_report(report_dir, report_name, report, api_key)
        if changed:
            raise GenerationPipelineError(
                f"{role} changed image-owned content"
            )

    log("generate", "START testcase_creator")
    testcase = _run(
        agent_runner=agent_runner,
        executable=executable,
        workspace=workspace,
        api_key=api_key,
        role="testcase_creator",
        report_dir=report_dir,
        prompt=build_role_prompt(
            role="testcase_creator",
            task=task,
            base_sha=base_sha,
        ),
    )
    _write_report(report_dir, "testcase-creator.json", testcase.payload, api_key)
    if testcase.payload.get("success") is not True:
        raise GenerationPipelineError("testcase_creator did not complete successfully")
    log("generate", "PASS testcase_creator")
    latest_testcase_payload: Mapping[str, object] = testcase.payload
    enforce_testcase_ownership(
        report_name="testcase-ownership.json",
        role="testcase_creator",
    )

    testcase_gate = enforce_gate(
        phase="full",
        report_name="precheck-gates.json",
        stage="generated_precheck",
        failure_message="deterministic target precheck did not pass",
        creator_payloads={
            "image_creator": latest_image_payload,
            "testcase_creator": latest_testcase_payload,
        },
    )
    testcase_findings = owner_gate_findings(
        testcase_gate,
        owner="testcase_creator",
    )
    if testcase_findings is not None:
        latest_testcase_payload = repair_deterministic_failure(
            creator_role="testcase_creator",
            report_name="testcase-creator-precheck-repair.json",
            findings={"gate": testcase_findings},
        )
        enforce_testcase_ownership(
            report_name="testcase-precheck-repair-ownership.json",
            role="testcase_creator deterministic repair",
        )
        enforce_gate(
            phase="full",
            report_name="precheck-repair-gates.json",
            stage="generated_precheck_repair",
            failure_message=(
                "deterministic target repair precheck did not pass"
            ),
            creator_payloads={
                "image_creator": latest_image_payload,
                "testcase_creator": latest_testcase_payload,
            },
        )

    def recheck_testcase_repair(payload: Mapping[str, object]) -> None:
        enforce_testcase_ownership(
            report_name="testcase-repair-ownership.json",
            role="testcase_creator repair",
        )
        enforce_gate(
            phase="full",
            report_name="testcase-repair-gates.json",
            stage="testcase_repair_precheck",
            failure_message=(
                "deterministic testcase repair precheck did not pass"
            ),
            creator_payloads={
                "image_creator": latest_image_payload,
                "testcase_creator": payload,
            },
        )

    testcase_review = _review_pair(
        creator_role="testcase_creator",
        qa_role="testcase_qa",
        agent_runner=agent_runner,
        executable=executable,
        workspace=workspace,
        report_dir=report_dir,
        task=task,
        base_sha=base_sha,
        api_key=api_key,
        post_repair_check=recheck_testcase_repair,
        creator_payload=latest_testcase_payload,
        resolve_evidence=lambda payload, round_number: resolve_creator_evidence(
            "testcase",
            payload,
            round_number,
        ),
    )
    if testcase_review.creator_payload is not None:
        latest_testcase_payload = testcase_review.creator_payload
    fix_rounds += testcase_review.fix_rounds
    if testcase_review.disagreement is not None:
        disagreements.append(testcase_review.disagreement)

    gate_report = enforce_gate(
        phase="full",
        report_name="gates.json",
        stage="target_contract",
        failure_message="deterministic target contract did not pass",
        creator_payloads={
            "image_creator": latest_image_payload,
            "testcase_creator": latest_testcase_payload,
        },
    )
    if disagreements:
        _write_report(
            report_dir,
            "qa-disagreements.json",
            {
                "status": "passed_with_qa_disagreement",
                "disagreements": disagreements,
            },
            api_key,
        )
    return GenerationResult(
        status="passed",
        qa_fix_rounds=fix_rounds,
        gate_report=gate_report,
        qa_disagreements=tuple(disagreements),
    )


def write_smoke_candidate(
    *,
    workspace: Path,
    task: TaskSpec,
) -> dict[str, str]:
    """Create the deterministic candidate used by the zero-AI pipeline check."""
    _require_phase1_task(task)
    workspace = Path(workspace)
    app = workspace / task.domain / task.app
    image = app / task.version / task.os_version
    tests = app / "tests"
    picture = app / "doc" / "picture"
    image.mkdir(parents=True)
    tests.mkdir(parents=True)
    picture.mkdir(parents=True)

    image_list = workspace / task.domain / "image-list.yml"
    image_list_data = yaml.safe_load(image_list.read_text())
    image_list_data["images"][task.app] = task.app
    image_list.write_text(yaml.safe_dump(image_list_data, sort_keys=False))

    (app / "meta.yml").write_text(
        f"{task.version}-oe2403sp4:\n"
        f"  path: {task.version}/{task.os_version}/Dockerfile\n"
    )
    (app / "README.md").write_text(
        "# Quick reference\n\n"
        "# Kvrocks | openEuler\n\n"
        "# Supported tags and respective Dockerfile links\n\n"
        f"{task.version}-oe2403sp4\n\n"
        "# Usage\n\n"
        f"docker run openeuler/kvrocks:{task.version}-oe2403sp4\n\n"
        "# Question and answering\n"
    )
    (app / "doc" / "image-info.yml").write_text(
        "name: kvrocks\n"
        "category: database\n"
        "description: Apache Kvrocks key-value database.\n"
        "environment: |\n"
        "  Docker on openEuler\n"
        "tags: |\n"
        "  2.16.0-oe2403sp4\n"
        "download: |\n"
        "  docker pull openeuler/kvrocks:{Tag}\n"
        "usage: |\n"
        "  docker run openeuler/kvrocks:{Tag}\n"
        "license: Apache-2.0\n"
        "similar_packages:\n"
        "  - Redis\n"
        "  - KeyDB\n"
        "  - Dragonfly\n"
        "dependency:\n"
        "  - N/A\n"
        "homepage: https://kvrocks.apache.org/\n"
        "upstream: https://github.com/apache/kvrocks\n"
    )
    (picture / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\npipeline-smoke")
    (image / "Dockerfile").write_text(
        f"ARG BASE=openeuler/openeuler:{task.os_version}\n"
        "FROM ${BASE} AS builder\n"
        f"ARG VERSION={task.version}\n"
        "WORKDIR /src/kvrocks\n"
        'RUN git clone --depth 1 --branch "v${VERSION}" '
        "https://github.com/apache/kvrocks.git . && ./x.py build -j 4\n"
        "FROM ${BASE}\n"
        "RUN dnf install -y redis && dnf clean all\n"
        "RUN groupadd -r kvrocks && "
        "useradd -r -g kvrocks kvrocks && "
        "mkdir -p /var/lib/kvrocks && "
        "chown -R kvrocks:kvrocks /var/lib/kvrocks\n"
        "COPY --from=builder /src/kvrocks/build/kvrocks "
        "/usr/local/bin/kvrocks\n"
        "USER kvrocks\n"
        "EXPOSE 6666\n"
        "HEALTHCHECK CMD redis-cli -p 6666 PING | grep PONG\n"
        'ENTRYPOINT ["kvrocks", "--bind", "0.0.0.0"]\n'
    )
    shared = tests / "test.sh"
    shared.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        ': "${EXPECTED_VERSION:?}"\n'
        'version_output="$(kvrocks --version)"\n'
        "read -r binary label reported_version details "
        '<<< "${version_output}"\n'
        'test "${binary}" = "kvrocks"\n'
        'test "${label}" = "version"\n'
        'test "${reported_version}" = "${EXPECTED_VERSION}"\n'
        'test -n "${details}"\n'
        'ping="$(redis-cli -p 6666 PING)"\n'
        'test "${ping}" = "PONG"\n'
        'test "$(id -u)" != 0\n'
    )
    shared.chmod(0o755)

    log("smoke", "PASS deterministic candidate")
    return {"status": "passed", "mode": "pipeline_smoke"}
