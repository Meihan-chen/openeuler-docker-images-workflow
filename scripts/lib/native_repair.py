"""Bounded Fixer loop around one native architecture validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from scripts.lib.agent_runtime import AgentResult, run_agent
from scripts.lib.failure_classification import classify_failure
from scripts.lib.generation_pipeline import build_role_prompt
from scripts.lib.native_validation import (
    NativeValidationError,
    validate_native_image,
)
from scripts.lib.oe_upgrade_sanitizer import (
    create_checkpoint,
    sanitize_agent_changes,
)
from scripts.lib.progress import log
from scripts.lib.task_spec import TaskSpec
from scripts.lib.target_contract import (
    TargetContractError,
    native_checks_pass,
    validate_add_version_target,
    validate_generated_target,
    validate_test_contract,
)


class NativeRepairError(RuntimeError):
    """Raised when bounded native repair cannot produce a valid candidate."""


@dataclass(frozen=True)
class NativeRepairResult:
    status: str
    repair_attempts: int
    report: Mapping[str, object]


def _load_failure_report(path: Path, error: Exception) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        payload = {"status": "failed", "failure": str(error)}
    if not isinstance(payload, dict):
        return {"status": "failed", "failure": str(error)}
    return payload


def _redact(value: object, secret: str) -> object:
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "REDACTED")
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, secret) for key, item in value.items()}
    return value


def _write_fixer_report(
    *,
    directory: Path,
    architecture: str,
    round_number: int,
    payload: Mapping[str, object],
    review: Mapping[str, object],
    api_key: str,
    attempt: int | None = None,
) -> None:
    safe_payload = dict(payload)
    safe_payload["_input_review"] = dict(review)
    safe_payload = _redact(safe_payload, api_key)
    suffix = "" if attempt is None else f"-attempt{attempt}"
    path = directory / (
        f"fixer-native-{architecture}-round{round_number}{suffix}.json"
    )
    path.write_text(
        json.dumps(safe_payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )


def _target_gate_report(
    *,
    target_validator: Callable[..., Mapping[str, object]],
    workspace: Path,
    task: TaskSpec,
    base_sha: str,
) -> Mapping[str, object]:
    try:
        validator = target_validator
        if task.scenario == "oe-upgrade" and validator is validate_generated_target:
            add_version = validate_add_version_target(
                repo=workspace,
                task=task,
                base_sha=base_sha,
            )
            tests = validate_test_contract(repo=workspace, task=task)
            return {
                **add_version,
                "status": (
                    "passed"
                    if tests.get("status") == "passed"
                    else "failed"
                ),
                "test_allowed": tests.get("test_allowed"),
                "runtime_test_allowed": tests.get("runtime_test_allowed"),
                "delivery_allowed": (
                    add_version.get("delivery_allowed") is True
                    and tests.get("status") == "passed"
                ),
                "findings": [
                    *list(add_version.get("findings") or []),
                    *list(tests.get("findings") or []),
                ],
                "errors": [
                    *list(add_version.get("errors") or []),
                    *list(tests.get("errors") or []),
                ],
            }
        return validator(
            repo=workspace,
            task=task,
            base_sha=base_sha,
        )
    except (TargetContractError, UnicodeError) as error:
        report: dict[str, object] = {
            "status": "failed",
            "build_allowed": False,
            "test_allowed": False,
            "delivery_allowed": False,
            "errors": str(error).splitlines(),
        }
        if isinstance(error, TargetContractError) and error.findings:
            report["findings"] = error.findings
        return report


@dataclass(frozen=True)
class RoundDecision:
    converged: bool
    round_number: int
    repair_attempts: int
    validated_patch_sha256: str
    terminal_status: str = ""


_ARCHITECTURES = ("x86_64", "aarch64")
# The gate is deterministic and free; a Docker round is neither. When the
# Fixer's edit violates the target contract, re-asking it here is far cheaper
# than spending another dual-architecture build to discover the same thing.
_GATE_REPAIR_ATTEMPTS = 3
_FIXER_RESPONSE_KEYS = (
    "success",
    "status",
    "diagnosis",
    "changes",
    "risks",
    "summary",
)


def _passed(report: Mapping[str, object]) -> bool:
    return (
        report.get("status") == "passed"
        and native_checks_pass(report.get("checks"))
    )


def _task_passed(task: TaskSpec, report: Mapping[str, object]) -> bool:
    return (
        report.get("status") == "passed"
        and native_checks_pass(
            report.get("checks"),
            oe_upgrade=task.scenario == "oe-upgrade",
        )
    )


def _format_checker_commit(report: Mapping[str, object]) -> str:
    result = report.get("format_check")
    if not isinstance(result, Mapping):
        return ""
    return str(result.get("commit_sha") or "")


def _native_gate_ready(gate: Mapping[str, object]) -> bool:
    """Build only when the image and its single runtime test are valid."""
    return (
        gate.get("status") == "passed"
        and gate.get("build_allowed") is True
        and gate.get("test_allowed") is True
        and gate.get("runtime_test_allowed") is True
    )


def _hard_stop_reasons(gate: Mapping[str, object]) -> list[str]:
    findings = gate.get("findings")
    hard = [
        finding
        for finding in findings
        if isinstance(finding, Mapping)
        and finding.get("level") == "hard_stop"
    ] if isinstance(findings, list) else []
    if hard:
        return [
            str(finding.get("code") or finding.get("message") or "hard_stop")
            for finding in hard
        ]
    if gate.get("status") != "passed" and gate.get("build_allowed") is False:
        errors = gate.get("errors")
        if isinstance(errors, list) and errors:
            return [str(error) for error in errors]
        return ["deterministic target contract"]
    return []


def _raise_for_invalid_gate(gate: Mapping[str, object]) -> None:
    reasons = _hard_stop_reasons(gate)
    if reasons:
        raise NativeRepairError("target contract hard stop: " + ", ".join(reasons))
    missing = [
        key
        for key in (
            "build_allowed",
            "test_allowed",
            "runtime_test_allowed",
        )
        if gate.get(key) is not True and gate.get(key) is not False
    ]
    if missing:
        raise NativeRepairError(
            "target contract omitted explicit permission fields: "
            + ", ".join(missing)
        )


def _needs_human_decision(
    *,
    report_dir: Path,
    round_number: int,
    repair_attempts: int,
    reports: Mapping[str, Mapping[str, object]],
    api_key: str,
    reason: str,
    architectures: tuple[str, ...] = _ARCHITECTURES,
    gate: Mapping[str, object] | None = None,
) -> RoundDecision:
    terminal: dict[str, object] = {
        "status": "needs-human-review",
        "reason": reason,
        "repair_attempts": repair_attempts,
        "round": round_number,
        "architectures": {
            name: _redact(dict(reports[name]), api_key)
            for name in architectures
        },
    }
    if gate is not None:
        terminal["gate"] = _redact(dict(gate), api_key)
    (report_dir / "convergence-report.json").write_text(
        json.dumps(terminal, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    return RoundDecision(
        converged=False,
        round_number=round_number,
        repair_attempts=repair_attempts,
        validated_patch_sha256="",
        terminal_status="needs-human-review",
    )


def _hard_stop_decision(
    *,
    report_dir: Path,
    round_number: int,
    repair_attempts: int,
    reports: Mapping[str, Mapping[str, object]],
    api_key: str,
    gate: Mapping[str, object],
    architectures: tuple[str, ...] = _ARCHITECTURES,
) -> RoundDecision:
    terminal = {
        "status": "hard-stop",
        "reason": "target-contract-hard-stop",
        "repair_attempts": repair_attempts,
        "round": round_number,
        "architectures": {
            name: _redact(dict(reports[name]), api_key)
            for name in architectures
        },
        "gate": _redact(dict(gate), api_key),
    }
    (report_dir / "convergence-report.json").write_text(
        json.dumps(terminal, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    return RoundDecision(
        converged=False,
        round_number=round_number,
        repair_attempts=repair_attempts,
        validated_patch_sha256="",
        terminal_status="hard-stop",
    )


def _classified(
    review: Mapping[str, object],
    *,
    task: TaskSpec,
    report: Mapping[str, object] | None = None,
    gate: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Attach what the harness already knows about the failure to the review.

    Without it the Fixer has to infer the repair from prose, which is how one
    stray tarball became 496 "change outside task scope" errors whose obvious
    reading pointed at the candidate instead of at the tarball.
    """
    classification = (
        _classify_report_failures(report, allowed_roots=(task.domain,))
        if report is not None and gate is None
        else classify_failure(
            report=report,
            gate=gate,
            allowed_roots=(task.domain,),
        )
    )
    return {
        **review,
        "classification": classification,
    }


def _classify_report_failures(
    report: Mapping[str, object],
    *,
    allowed_roots: tuple[str, ...],
) -> dict[str, object]:
    """Classify every captured check failure, preserving the primary shape."""
    primary = classify_failure(report=report, allowed_roots=allowed_roots)
    failures = report.get("failures")
    failures = failures if isinstance(failures, list) else []
    classified = []
    for failure in failures:
        if not isinstance(failure, Mapping):
            continue
        isolated = {
            **report,
            "failed_stage": failure.get("stage", ""),
            "failure": failure.get("failure", ""),
            "failure_details": failure.get("failure_details", {}),
        }
        isolated.pop("failures", None)
        result = classify_failure(
            report=isolated,
            allowed_roots=allowed_roots,
        )
        classified.append(
            {
                "stage": str(failure.get("stage", "")),
                "check": str(failure.get("check", "")),
                **result,
            }
        )
    format_check = report.get("format_check")
    if (
        isinstance(format_check, Mapping)
        and format_check.get("status") == "failed"
        and not any(item["stage"] == "upstream_format" for item in classified)
    ):
        isolated = {
            **report,
            "failed_stage": "upstream_format",
            "failure": (
                format_check.get("failure")
                or format_check.get("output")
                or "upstream format check failed"
            ),
            "failure_details": {
                key: format_check[key]
                for key in ("kind", "stage", "commit_sha")
                if key in format_check
            },
        }
        isolated.pop("failures", None)
        classified.append(
            {
                "stage": "upstream_format",
                "check": "upstream_format",
                **classify_failure(
                    report=isolated,
                    allowed_roots=allowed_roots,
                ),
            }
        )
    return {**primary, "failures": classified} if classified else primary


def _all_failures_are_infra(classification: Mapping[str, object]) -> bool:
    failures = classification.get("failures")
    if isinstance(failures, list) and failures:
        return all(
            isinstance(failure, Mapping)
            and failure.get("category") == "infra"
            for failure in failures
        )
    return classification.get("category") == "infra"


def aggregate_round_classifications(
    per_architecture: Mapping[str, Mapping[str, object]],
) -> dict[str, bool]:
    """Reduce deterministic per-architecture ownership into a round route."""
    categories: list[str] = []
    for result in per_architecture.values():
        failures = result.get("failures")
        if isinstance(failures, list) and failures:
            categories.extend(
                str(failure.get("category", "unclassified"))
                for failure in failures
                if isinstance(failure, Mapping)
            )
        else:
            categories.append(str(result.get("category", "unclassified")))
    actionable = {
        "image-contract",
        "test-contract",
        "build-error",
        "runtime-error",
        "os-identity",
    }
    dependency = {"dependency-unavailable"}
    return {
        "infrastructure_only": bool(categories)
        and all(category == "infra" for category in categories),
        "dependency_unavailable": any(
            category in dependency for category in categories
        ),
        "has_actionable_candidate_evidence": any(
            category in actionable for category in categories
        ),
    }


def _resolve_full_evidence(
    evidence_roots: Mapping[str, Path] | None,
    *,
    workspace: Path,
) -> tuple[dict[str, object], tuple[Path, ...]]:
    if not evidence_roots:
        return {}, ()
    review: dict[str, object] = {}
    readable: list[Path] = []
    for architecture in _ARCHITECTURES:
        raw_root = evidence_roots.get(architecture)
        if raw_root is None:
            continue
        root = Path(raw_root).resolve()
        if not root.is_dir():
            raise NativeRepairError(
                f"native evidence directory does not exist: {root}"
            )
        if root == workspace or workspace in root.parents:
            raise NativeRepairError(
                "native evidence directories must remain outside the target workspace"
            )
        files: list[str] = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise NativeRepairError(
                    f"native evidence must not contain symlinks: {path}"
                )
            if path.is_file():
                resolved = path.resolve()
                if root not in resolved.parents:
                    raise NativeRepairError(
                        f"native evidence escaped its declared root: {path}"
                    )
                files.append(str(resolved))
        review[architecture] = {
            "root": str(root),
            "files": files,
        }
        readable.append(root)
    return review, tuple(readable)


def decide_round(
    *,
    workspace: Path,
    task: TaskSpec,
    base_sha: str,
    round_number: int,
    max_rounds: int,
    reports: Mapping[str, Mapping[str, object]],
    report_dir: Path,
    executable: Path,
    api_key: str,
    evidence_roots: Mapping[str, Path] | None = None,
    agent_runner: Callable[..., AgentResult] = run_agent,
    target_validator: Callable[..., Mapping[str, object]] = (
        validate_generated_target
    ),
    checkpoint_factory: Callable[..., object] = create_checkpoint,
    sanitizer: Callable[..., object] = sanitize_agent_changes,
) -> RoundDecision:
    """Converge one parallel round, or repair once for the next one.

    Both architectures validate the same candidate in every round, so the
    round that passes on both is itself the proof that one candidate builds
    and runs everywhere. That is why no separate final revalidation is needed.
    """
    workspace = Path(workspace).resolve()
    report_dir = Path(report_dir).resolve()
    if report_dir == workspace or workspace in report_dir.parents:
        raise NativeRepairError(
            "native repair evidence must remain outside target workspace"
        )
    architectures = (
        task.architectures
        if task.schema_version == 2
        else _ARCHITECTURES
    )
    missing = [name for name in architectures if name not in reports]
    if missing:
        raise NativeRepairError(
            "round decision is missing required architectures: "
            + ", ".join(missing)
        )
    if task.schema_version == 2:
        wrong_tasks = [
            name
            for name in architectures
            if reports[name].get("task_key") != task.task_key
        ]
        if wrong_tasks:
            raise NativeRepairError(
                "native reports have a mismatched task_key: "
                + ", ".join(wrong_tasks)
            )
        wrong_architectures = [
            name
            for name in architectures
            if reports[name].get("architecture") != name
        ]
        if wrong_architectures:
            raise NativeRepairError(
                "native reports have a mismatched architecture: "
                + ", ".join(wrong_architectures)
            )
    report_dir.mkdir(parents=True, exist_ok=True)
    stage = f"round:{round_number}"
    full_evidence, external_read_dirs = _resolve_full_evidence(
        evidence_roots,
        workspace=workspace,
    )

    invalid_passed = [
        name
        for name in architectures
        if reports[name].get("status") == "passed"
        and not native_checks_pass(
            reports[name].get("checks"),
            oe_upgrade=task.scenario == "oe-upgrade",
        )
    ]
    if invalid_passed:
        raise NativeRepairError(
            "native report checks are incomplete: "
            + ", ".join(invalid_passed)
        )

    format_commits = [
        _format_checker_commit(reports[name]) for name in architectures
    ]
    if all(format_commits) and len(set(format_commits)) != 1:
        if max_rounds == 0 or round_number > max_rounds:
            raise NativeRepairError(
                "format checker infrastructure retries were exhausted"
            )
        log(stage, "SKIP fixer reason=format-checker-commit-mismatch")
        return RoundDecision(
            converged=False,
            round_number=round_number,
            repair_attempts=0,
            validated_patch_sha256="",
        )

    if all(_task_passed(task, reports[name]) for name in architectures):
        digests = {
            str(reports[name].get("validated_patch_sha256", ""))
            for name in architectures
        }
        if len(digests) != 1 or not digests.pop():
            raise NativeRepairError(
                "both architectures passed but did not record the same "
                "validated candidate"
            )
        log(stage, "PASS converged")
        return RoundDecision(
            converged=True,
            round_number=round_number,
            repair_attempts=0,
            validated_patch_sha256=str(
                reports[architectures[0]]["validated_patch_sha256"]
            ),
        )

    per_architecture = {
        name: _classify_report_failures(
            reports[name],
            allowed_roots=(task.domain,),
        )
        for name in architectures
        if not _task_passed(task, reports[name])
    }
    round_classification = aggregate_round_classifications(per_architecture)
    if task.scenario == "oe-upgrade":
        if round_classification["infrastructure_only"]:
            return RoundDecision(
                converged=False,
                round_number=round_number,
                repair_attempts=0,
                validated_patch_sha256="",
                terminal_status="failed:infrastructure",
            )
        if round_classification["dependency_unavailable"]:
            return RoundDecision(
                converged=False,
                round_number=round_number,
                repair_attempts=0,
                validated_patch_sha256="",
                terminal_status="failed:dependency",
            )
        if not round_classification["has_actionable_candidate_evidence"]:
            return RoundDecision(
                converged=False,
                round_number=round_number,
                repair_attempts=0,
                validated_patch_sha256="",
                terminal_status="failed:evidence-insufficient",
            )
        if round_number > max_rounds:
            return RoundDecision(
                converged=False,
                round_number=round_number,
                repair_attempts=max_rounds,
                validated_patch_sha256="",
                terminal_status="failed:repair-exhausted",
            )
    if max_rounds == 0:
        raise NativeRepairError("zero-repair validation failed")
    if round_number > max_rounds:
        if all(
            _all_failures_are_infra(result)
            for result in per_architecture.values()
        ):
            raise NativeRepairError(
                "native validation infrastructure retries were exhausted"
            )
        log(stage, "NEEDS_HUMAN repair budget exhausted")
        return _needs_human_decision(
            report_dir=report_dir,
            round_number=round_number,
            repair_attempts=max_rounds,
            reports=reports,
            api_key=api_key,
            reason="native-repair-budget-exhausted",
            architectures=architectures,
        )

    if not api_key:
        # Converging needs no model, so the key is only required once a repair
        # is actually about to run.
        raise NativeRepairError("DEEPSEEK_API_KEY is required to repair")
    # One Fixer sees both architectures, so a fix for one cannot silently
    # regress the other — and for the same reason one category cannot speak
    # for both.
    if all(
        _all_failures_are_infra(result)
        for result in per_architecture.values()
    ):
        # The Fixer would be told to change nothing, so asking it buys nothing.
        # The round is still consumed, which keeps the retry bounded.
        log(stage, "SKIP fixer reason=infra")
        return RoundDecision(
            converged=False,
            round_number=round_number,
            repair_attempts=0,
            validated_patch_sha256="",
        )
    review: dict[str, object] = {
        "kind": "native_validation_failure",
        "repair_round": round_number,
        "classification": per_architecture,
        "architectures": {
            name: _redact(dict(reports[name]), api_key)
            for name in architectures
        },
    }
    if full_evidence:
        review["full_evidence"] = full_evidence
    for attempt in range(1, _GATE_REPAIR_ATTEMPTS + 1):
        checkpoint = None
        if task.scenario == "oe-upgrade":
            checkpoint = checkpoint_factory(
                workspace=workspace,
                base_sha=base_sha,
                task=task,
                destination=(
                    report_dir
                    / "checkpoints"
                    / f"round{round_number}-attempt{attempt}"
                ),
                round_number=round_number,
                agent_role="code-fixer",
            )
            review = {
                **review,
                "checkpoint_id": getattr(checkpoint, "checkpoint_id", ""),
                "allowed_paths": list(
                    getattr(checkpoint, "allowed_paths", ())
                ),
            }
        log(stage, f"START fixer attempt={attempt}")
        fixed = agent_runner(
            executable=executable,
            role="fixer",
            prompt=build_role_prompt(
                role="fixer",
                task=task,
                base_sha=base_sha,
                review=review,
                workspace=workspace,
            ),
            workspace=workspace,
            api_key=api_key,
            required_keys=("success", "changes"),
            response_keys=_FIXER_RESPONSE_KEYS,
            timeout=7200,
            external_read_dirs=external_read_dirs,
        )
        _write_fixer_report(
            directory=report_dir,
            architecture="dual",
            round_number=round_number,
            payload=fixed.payload,
            review=review,
            api_key=api_key,
            attempt=attempt,
        )
        if checkpoint is not None:
            try:
                sanitization = sanitizer(
                    workspace=workspace,
                    base_sha=base_sha,
                    task=task,
                    checkpoint=checkpoint,
                    report_path=(
                        report_dir
                        / f"sanitization-round{round_number}-attempt{attempt}.json"
                    ),
                )
            except Exception as error:
                log(stage, f"HARD_STOP sanitization: {error}")
                return RoundDecision(
                    converged=False,
                    round_number=round_number,
                    repair_attempts=attempt,
                    validated_patch_sha256="",
                    terminal_status="failed:contract",
                )
            if getattr(sanitization, "clean", False) is not True:
                return RoundDecision(
                    converged=False,
                    round_number=round_number,
                    repair_attempts=attempt,
                    validated_patch_sha256="",
                    terminal_status="failed:contract",
                )
        gate = _target_gate_report(
            target_validator=target_validator,
            workspace=workspace,
            task=task,
            base_sha=base_sha,
        )
        hard_stop_reasons = _hard_stop_reasons(gate)
        if hard_stop_reasons:
            log(
                stage,
                "HARD_STOP target_contract "
                + ",".join(hard_stop_reasons),
            )
            return _hard_stop_decision(
                report_dir=report_dir,
                round_number=round_number,
                repair_attempts=attempt,
                reports=reports,
                api_key=api_key,
                gate=gate,
                architectures=architectures,
            )
        _raise_for_invalid_gate(gate)
        if fixed.payload.get("success") is not True:
            raise NativeRepairError(
                f"fixer failed in round {round_number} attempt {attempt}"
            )
        if _native_gate_ready(gate):
            log(stage, f"PASS fixer attempt={attempt}")
            return RoundDecision(
                converged=False,
                round_number=round_number,
                repair_attempts=attempt,
                validated_patch_sha256="",
            )
        log(stage, f"NEEDS_FIX target_contract attempt={attempt}")
        review = _classified({**review, "gate": gate}, task=task, gate=gate)

    log(stage, "NEEDS_HUMAN target contract repair exhausted")
    return _needs_human_decision(
        report_dir=report_dir,
        round_number=round_number,
        repair_attempts=_GATE_REPAIR_ATTEMPTS,
        reports=reports,
        api_key=api_key,
        reason="target-contract-repair-exhausted",
        architectures=architectures,
        gate=gate,
    )


def validate_native_with_repairs(
    *,
    workspace: Path,
    task: TaskSpec,
    base_sha: str,
    architecture: str,
    run_id: str,
    report_path: Path,
    junit_path: Path,
    repair_report_dir: Path,
    executable: Path,
    api_key: str,
    max_repairs: int = 3,
    native_validator: Callable[..., Mapping[str, object]] = (
        validate_native_image
    ),
    agent_runner: Callable[..., AgentResult] = run_agent,
    target_validator: Callable[..., Mapping[str, object]] = (
        validate_generated_target
    ),
) -> NativeRepairResult:
    workspace = Path(workspace).resolve()
    report_path = Path(report_path)
    junit_path = Path(junit_path)
    repair_report_dir = Path(repair_report_dir).resolve()
    if max_repairs != 3:
        raise NativeRepairError("phase one requires exactly 3 repair attempts")
    if repair_report_dir == workspace or workspace in repair_report_dir.parents:
        raise NativeRepairError(
            "native repair evidence must remain outside target workspace"
        )
    if not api_key:
        raise NativeRepairError("DEEPSEEK_API_KEY is required")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    repair_report_dir.mkdir(parents=True, exist_ok=True)

    repair_attempts = 0
    initial_gate = _target_gate_report(
        target_validator=target_validator,
        workspace=workspace,
        task=task,
        base_sha=base_sha,
    )
    _raise_for_invalid_gate(initial_gate)
    pending_review: Mapping[str, object] | None = None
    if not _native_gate_ready(initial_gate):
        pending_review = _classified(
            {
                "kind": "deterministic_target_contract",
                "architecture": architecture,
                "gate": initial_gate,
            },
            task=task,
            gate=initial_gate,
        )
    native_failure: object = None
    while True:
        if pending_review is None:
            try:
                report = native_validator(
                    workspace=workspace,
                    task=task,
                    architecture=architecture,
                    run_id=run_id,
                    report_path=report_path,
                    junit_path=junit_path,
                )
            except NativeValidationError as error:
                native_failure = _redact(
                    _load_failure_report(report_path, error),
                    api_key,
                )
                pending_review = _classified(
                    {
                        "kind": "native_validation_failure",
                        "architecture": architecture,
                        "report": native_failure,
                    },
                    task=task,
                    report=(
                        native_failure
                        if isinstance(native_failure, Mapping)
                        else None
                    ),
                )
            else:
                if not _passed(report):
                    raise NativeRepairError(
                        "native validator returned without complete passed checks"
                    )
                summary = {
                    "architecture": architecture,
                    "repair_attempts": repair_attempts,
                    "status": "passed",
                }
                (
                    repair_report_dir
                    / f"native-repair-{architecture}.json"
                ).write_text(
                    json.dumps(
                        summary,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                )
                return NativeRepairResult(
                    status="passed",
                    repair_attempts=repair_attempts,
                    report=report,
                )

        if pending_review is not None:
            if repair_attempts == max_repairs:
                kind = str(pending_review.get("kind", "native validation"))
                terminal = {
                    "status": "needs-human-review",
                    "reason": "target-contract-repair-exhausted",
                    "architecture": architecture,
                    "repair_attempts": repair_attempts,
                    "last_review": _redact(dict(pending_review), api_key),
                }
                (repair_report_dir / "convergence-report.json").write_text(
                    json.dumps(
                        terminal,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                )
                log(
                    f"native:{architecture}",
                    f"NEEDS_HUMAN {kind} repair exhausted",
                )
                return NativeRepairResult(
                    status="needs-human-review",
                    repair_attempts=repair_attempts,
                    report=terminal,
                )
            repair_attempts += 1
            review = dict(pending_review)
            review["repair_round"] = repair_attempts
            log(
                f"native:{architecture}",
                f"START fixer round={repair_attempts} "
                f"reason={review['kind']}",
            )
            fixed = agent_runner(
                executable=executable,
                role="fixer",
                prompt=build_role_prompt(
                    role="fixer",
                    task=task,
                    base_sha=base_sha,
                    review=review,
                    workspace=workspace,
                ),
                workspace=workspace,
                api_key=api_key,
                required_keys=("success", "changes"),
                response_keys=_FIXER_RESPONSE_KEYS,
            )
            _write_fixer_report(
                directory=repair_report_dir,
                architecture=architecture,
                round_number=repair_attempts,
                payload=fixed.payload,
                review=review,
                api_key=api_key,
            )
            if fixed.payload.get("success") is not True:
                raise NativeRepairError(
                    f"fixer failed for {architecture} round "
                    f"{repair_attempts}"
                )
            gate = _target_gate_report(
                target_validator=target_validator,
                workspace=workspace,
                task=task,
                base_sha=base_sha,
            )
            _raise_for_invalid_gate(gate)
            if not _native_gate_ready(gate):
                log(
                    f"native:{architecture}",
                    f"NEEDS_FIX target_contract round={repair_attempts}",
                )
                pending_review = _classified(
                    {
                        "kind": "deterministic_target_contract",
                        "architecture": architecture,
                        "native_failure": native_failure,
                        "gate": gate,
                    },
                    task=task,
                    gate=gate,
                )
                continue
            log(
                f"native:{architecture}",
                f"PASS fixer round={repair_attempts}",
            )
            pending_review = None
            continue
