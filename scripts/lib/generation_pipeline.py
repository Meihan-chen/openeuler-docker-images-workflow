"""Adversarial Agent generation stage followed by deterministic target gates."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import yaml

from scripts.lib.agent_runtime import AgentResult, AgentRuntimeError, run_agent
from scripts.lib.progress import log
from scripts.lib.task_spec import TaskSpec
from scripts.lib.target_contract import TargetContractError, validate_generated_target


class GenerationPipelineError(RuntimeError):
    """Raised when an Agent pair or deterministic gate fails closed."""


_PROMPT_DIR = Path(__file__).resolve().parents[2] / ".github" / "agents"
_PROMPT_FILES = {
    "image_creator": "image-creator.md",
    "image_qa": "image-qa.md",
    "testcase_creator": "testcase-creator.md",
    "testcase_qa": "testcase-qa.md",
    "fixer": "code-fixer.md",
}
_REQUIRED_KEYS = {
    "image_creator": ("success", "files_created"),
    "image_qa": ("status", "issues", "summary"),
    "testcase_creator": ("success", "files_created"),
    "testcase_qa": ("status", "issues", "coverage_score", "summary"),
    "fixer": ("success", "changes"),
}
_QA_TIMEOUT_SECONDS = 180
_DEFAULT_AGENT_TIMEOUT_SECONDS = 1800
_QA_SNAPSHOT_MAX_CHARS = 64_000
_PHASE1_TASK = (
    "kvrocks",
    "2.16.0",
    "24.03-lts-sp4",
    "Database",
    "https://github.com/apache/kvrocks/tree/v2.16.0",
)


@dataclass(frozen=True)
class GenerationResult:
    status: str
    qa_fix_rounds: int
    gate_report: Mapping[str, object]


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
                "--ignore",
                "DL3041",
                str(dockerfile),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        return {
            "status": "failed",
            "returncode": None,
            "output": str(error),
        }
    output = "\n".join(
        part.strip()
        for part in (completed.stdout, completed.stderr)
        if part.strip()
    )
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "output": output,
    }


def _require_phase1_task(task: TaskSpec) -> None:
    actual = (
        task.app,
        task.version,
        task.os_version,
        task.domain,
        task.source_url,
    )
    if actual != _PHASE1_TASK:
        raise GenerationPipelineError(
            "phase one only supports the confirmed Kvrocks 2.16.0 "
            "TaskSpec"
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
        app_root / "tests" / "goss.yaml",
        app_root / "tests" / "goss_wait.yaml",
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
        "- Do not modify any other path.",
    ]
    if role in {"testcase_creator", "testcase_qa", "fixer"}:
        contract_lines.extend(
            (
                "- Every in-container readiness probe must use commands "
                "available in the runtime image; infer availability from "
                "the Dockerfile and do not assume host diagnostic tools.",
                "- Derive application-specific tests from the Dockerfile and "
                "official upstream behavior; do not copy another "
                "application's commands, ports or user assertions.",
                "- The native harness executes shared tests inside an "
                "already-running container; test scripts must not invoke "
                "Docker or own the container lifecycle.",
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
                f"{app_root}/tests/goss.yaml",
                f"{app_root}/tests/goss_wait.yaml",
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
            )
        )
    contract_lines.extend(
        (
            "- Do not install or upgrade host tools or packages with brew, "
            "apt, dnf, yum, pip, or similar commands.",
            "- Do not run Docker builds or invoke linters; the harness runs "
            "those validations after your response.",
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
) -> str:
    app_root = workspace / task.domain / task.app
    image_root = app_root / task.version / task.os_version
    if role == "image_qa":
        tests_root = app_root / "tests"
        paths = [
            path
            for path in _candidate_paths(
                workspace=workspace,
                task=task,
            )
            if tests_root not in path.parents
        ]
    else:
        tests_root = app_root / "tests"
        paths = [
            image_root / "Dockerfile",
            tests_root / "goss.yaml",
            tests_root / "goss_wait.yaml",
            tests_root / "test_helpers.sh",
            tests_root / "test.sh",
        ]
        if tests_root.is_dir():
            paths.extend(
                sorted(
                    path
                    for path in tests_root.iterdir()
                    if path.is_file() and path not in paths
                )
            )

    snapshot: dict[str, str] = {}
    for path in paths:
        relative = str(path.relative_to(workspace))
        if not path.is_file():
            snapshot[relative] = "<missing>"
            continue
        if path.suffix.lower() == ".png":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot[relative] = (
                f"<binary file: {path.stat().st_size} bytes, sha256:{digest}>"
            )
        else:
            snapshot[relative] = path.read_text(errors="replace")

    encoded_snapshot = json.dumps(
        snapshot,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    if len(encoded_snapshot) > _QA_SNAPSHOT_MAX_CHARS:
        raise GenerationPipelineError(
            "candidate snapshot is too large for bounded QA"
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
    return (
        build_role_prompt(role=role, task=task, base_sha=base_sha)
        + previous_findings
        + "\n## Embedded candidate snapshot\n\n"
        + "Review only the file snapshot below. Do not call tools or read the "
        "workspace; return the documented JSON review contract directly.\n\n"
        + "```json\n"
        + encoded_snapshot
        + "\n```\n"
    )


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
        return {
            "status": "failed",
            "errors": str(error).splitlines(),
        }


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
    try:
        return agent_runner(
            executable=executable,
            role=role,
            prompt=prompt,
            workspace=workspace,
            api_key=api_key,
            required_keys=_REQUIRED_KEYS[role],
            timeout=(
                _QA_TIMEOUT_SECONDS
                if role in {"image_qa", "testcase_qa"}
                else _DEFAULT_AGENT_TIMEOUT_SECONDS
            ),
        )
    except AgentRuntimeError as error:
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
        raise GenerationPipelineError(f"{role} Agent failed: {error}") from error


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
    post_repair_check: Callable[[], None] | None = None,
) -> int:
    def qa_prompt(
        previous_review: Mapping[str, object] | None = None,
    ) -> str:
        try:
            return _qa_prompt(
                role=qa_role,
                workspace=workspace,
                task=task,
                base_sha=base_sha,
                previous_review=previous_review,
            )
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
    review = _run(
        agent_runner=agent_runner,
        executable=executable,
        workspace=workspace,
        api_key=api_key,
        role=qa_role,
        report_dir=report_dir,
        prompt=qa_prompt(),
    )
    _write_report(report_dir, f"{qa_role.replace('_', '-')}-round1.json", review.payload, api_key)
    _log_review_result(
        qa_role=qa_role,
        round_number=1,
        payload=review.payload,
        api_key=api_key,
    )
    if review.payload.get("status") == "approved":
        log("review", f"PASS {qa_role} round=1")
        return 0
    if review.payload.get("status") != "needs_fix":
        raise GenerationPipelineError(f"{qa_role} returned an invalid status")

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
            review=review.payload,
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
        post_repair_check()

    log("review", f"START {qa_role} round=2")
    second = _run(
        agent_runner=agent_runner,
        executable=executable,
        workspace=workspace,
        api_key=api_key,
        role=qa_role,
        report_dir=report_dir,
        prompt=qa_prompt(review.payload),
    )
    _write_report(
        report_dir,
        f"{qa_role.replace('_', '-')}-round2.json",
        second.payload,
        api_key,
    )
    _log_review_result(
        qa_role=qa_role,
        round_number=2,
        payload=second.payload,
        api_key=api_key,
    )
    second_status = second.payload.get("status")
    if second_status == "approved":
        log("review", f"PASS {qa_role} round=2")
    elif second_status == "needs_fix":
        log(
            "review",
            f"DISAGREEMENT {qa_role} round=2; continue=local_validation",
        )
    else:
        raise GenerationPipelineError(f"{qa_role} returned an invalid status")
    return 1


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
) -> GenerationResult:
    workspace = Path(workspace).resolve()
    report_dir = Path(report_dir).resolve()
    _require_phase1_task(task)
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
    testcase_owned = {
        app_root / "tests" / "goss.yaml",
        app_root / "tests" / "goss_wait.yaml",
        app_root / "tests" / "test_helpers.sh",
        app_root / "tests" / "test.sh",
    }

    def enforce_gate(
        *,
        phase: str,
        report_name: str,
        stage: str,
        failure_message: str,
        fail_closed: bool = True,
    ) -> Mapping[str, object]:
        log("gate", f"START {stage}")
        report = _target_gate_report(
            target_validator=target_validator,
            workspace=workspace,
            task=task,
            base_sha=base_sha,
            phase=phase,
        )
        _write_report(report_dir, report_name, report, api_key)
        if report.get("status") != "passed":
            if fail_closed:
                raise GenerationPipelineError(failure_message)
            log("gate", f"NEEDS_FIX {stage}")
            return report
        log("gate", f"PASS {stage}")
        return report

    def lint_image(
        *,
        report_name: str,
        stage: str,
        fail_closed: bool = True,
    ) -> Mapping[str, object]:
        if image_linter is None:
            return {"status": "passed"}
        log("lint", f"START {stage}")
        report = image_linter(dockerfile)
        _write_report(report_dir, report_name, report, api_key)
        if report.get("status") != "passed":
            if fail_closed:
                raise GenerationPipelineError(f"{stage} did not pass")
            log("lint", f"NEEDS_FIX {stage}")
            return report
        log("lint", f"PASS {stage}")
        return report

    def image_owned_snapshot() -> dict[str, str]:
        return {
            str(path.relative_to(workspace)): (
                f"{path.stat().st_mode & 0o777:o}:"
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}"
            )
            for path in _candidate_paths(
                workspace=workspace,
                task=task,
            )
            if path.is_file() and path not in testcase_owned
        }

    def repair_deterministic_failure(
        *,
        creator_role: str,
        report_name: str,
        findings: Mapping[str, object],
    ) -> None:
        validation_report = {
            "status": "needs_fix",
            "issues": [
                {
                    "severity": "blocker",
                    "description": "Deterministic validation failed.",
                    "findings": findings,
                }
            ],
            "summary": (
                "Fix only the deterministic validation findings, "
                "then return the required Creator result."
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

    image_gate = enforce_gate(
        phase="image",
        report_name="image-precheck-gates.json",
        stage="image_precheck",
        failure_message="deterministic image precheck did not pass",
        fail_closed=False,
    )
    image_lint: Mapping[str, object] = {
        "status": "skipped",
        "reason": "image gate did not pass",
    }
    if image_gate.get("status") == "passed":
        image_lint = lint_image(
            report_name="image-lint.json",
            stage="image_lint",
            fail_closed=False,
        )
    if (
        image_gate.get("status") != "passed"
        or image_lint.get("status") != "passed"
    ):
        repair_deterministic_failure(
            creator_role="image_creator",
            report_name="image-creator-precheck-repair.json",
            findings={"gate": image_gate, "lint": image_lint},
        )
        enforce_gate(
            phase="image",
            report_name="image-precheck-repair-gates.json",
            stage="image_precheck_repair",
            failure_message=(
                "deterministic image repair precheck did not pass"
            ),
        )
        lint_image(
            report_name="image-precheck-repair-lint.json",
            stage="image_lint_repair",
        )

    def recheck_image_repair() -> None:
        enforce_gate(
            phase="image",
            report_name="image-repair-gates.json",
            stage="image_repair_precheck",
            failure_message=(
                "deterministic image repair precheck did not pass"
            ),
        )
        lint_image(
            report_name="image-repair-lint.json",
            stage="image_repair_lint",
        )

    fix_rounds = _review_pair(
        creator_role="image_creator",
        qa_role="image_qa",
        agent_runner=agent_runner,
        executable=executable,
        workspace=workspace,
        report_dir=report_dir,
        task=task,
        base_sha=base_sha,
        api_key=api_key,
        post_repair_check=recheck_image_repair,
    )
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
    enforce_testcase_ownership(
        report_name="testcase-ownership.json",
        role="testcase_creator",
    )

    testcase_gate = enforce_gate(
        phase="full",
        report_name="precheck-gates.json",
        stage="generated_precheck",
        failure_message="deterministic target precheck did not pass",
        fail_closed=False,
    )
    if testcase_gate.get("status") != "passed":
        repair_deterministic_failure(
            creator_role="testcase_creator",
            report_name="testcase-creator-precheck-repair.json",
            findings={"gate": testcase_gate},
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
        )

    def recheck_testcase_repair() -> None:
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
        )

    fix_rounds += _review_pair(
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
    )

    gate_report = enforce_gate(
        phase="full",
        report_name="gates.json",
        stage="target_contract",
        failure_message="deterministic target contract did not pass",
    )
    return GenerationResult(
        status="passed",
        qa_fix_rounds=fix_rounds,
        gate_report=gate_report,
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
        "environment: Docker on openEuler\n"
        "tags: 2.16.0-oe2403sp4\n"
        "download: docker pull openeuler/kvrocks:{Tag}\n"
        "usage: docker run openeuler/kvrocks:{Tag}\n"
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
        "RUN groupadd --non-unique --gid 999 kvrocks && "
        "useradd --non-unique --uid 999 --gid kvrocks kvrocks && "
        "mkdir -p /var/lib/kvrocks && "
        "chown -R 999:999 /var/lib/kvrocks\n"
        "COPY --from=builder /src/kvrocks/build/kvrocks "
        "/usr/local/bin/kvrocks\n"
        "USER 999\n"
        "EXPOSE 6666\n"
        "HEALTHCHECK CMD redis-cli -p 6666 PING | grep PONG\n"
        'ENTRYPOINT ["kvrocks", "--bind", "0.0.0.0"]\n'
    )
    (tests / "goss.yaml").write_text(
        "port:\n"
        "  tcp:6666:\n"
        "    listening: true\n"
        "command:\n"
        "  version:\n"
        "    exec: kvrocks --version\n"
        "    stdout:\n"
        '      - "{{.Env.EXPECTED_VERSION}}"\n'
        "  ping:\n"
        "    exec: redis-cli -p 6666 PING\n"
        "    stdout:\n"
        "      - PONG\n"
    )
    (tests / "goss_wait.yaml").write_text(
        "port:\n  tcp:6666:\n    listening: true\n"
    )
    helpers = tests / "test_helpers.sh"
    helpers.write_text(
        "#!/bin/bash\n"
        "wait_for_kvrocks() { redis-cli -p 6666 PING; }\n"
    )
    shared = tests / "test.sh"
    shared.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        ': "${EXPECTED_VERSION:?}"\n'
        'kvrocks --version | grep -F "${EXPECTED_VERSION}"\n'
        "redis-cli -p 6666 PING | grep -F PONG\n"
        'test "$(id -u)" = 999\n'
    )
    for script in (helpers, shared):
        script.chmod(0o755)

    log("smoke", "PASS deterministic candidate")
    return {"status": "passed", "mode": "pipeline_smoke"}
