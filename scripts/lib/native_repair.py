"""Bounded Fixer loop around one native architecture validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from scripts.lib.agent_runtime import AgentResult, run_agent
from scripts.lib.generation_pipeline import build_role_prompt
from scripts.lib.native_validation import (
    NativeValidationError,
    validate_native_image,
)
from scripts.lib.progress import log
from scripts.lib.task_spec import TaskSpec
from scripts.lib.target_contract import (
    TargetContractError,
    validate_generated_target,
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
) -> None:
    safe_payload = dict(payload)
    safe_payload["_input_review"] = dict(review)
    safe_payload = _redact(safe_payload, api_key)
    path = directory / (
        f"fixer-native-{architecture}-round{round_number}.json"
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
        return target_validator(
            repo=workspace,
            task=task,
            base_sha=base_sha,
        )
    except (TargetContractError, UnicodeError) as error:
        return {
            "status": "failed",
            "errors": str(error).splitlines(),
        }


def validate_native_with_repairs(
    *,
    workspace: Path,
    task: TaskSpec,
    base_sha: str,
    architecture: str,
    run_id: str,
    dgoss: Path,
    goss: Path,
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
    pending_review: Mapping[str, object] | None = None
    if initial_gate.get("status") != "passed":
        pending_review = {
            "kind": "deterministic_target_contract",
            "architecture": architecture,
            "gate": initial_gate,
        }
    native_failure: object = None
    while True:
        if pending_review is None:
            try:
                report = native_validator(
                    workspace=workspace,
                    task=task,
                    architecture=architecture,
                    run_id=run_id,
                    dgoss=dgoss,
                    goss=goss,
                    report_path=report_path,
                    junit_path=junit_path,
                )
            except NativeValidationError as error:
                native_failure = _redact(
                    _load_failure_report(report_path, error),
                    api_key,
                )
                pending_review = {
                    "kind": "native_validation_failure",
                    "architecture": architecture,
                    "report": native_failure,
                }
            else:
                if report.get("status") != "passed":
                    raise NativeRepairError(
                        "native validator returned without passed status"
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
                raise NativeRepairError(
                    f"{kind.replace('_', ' ')} failed after "
                    f"{max_repairs} repair attempts"
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
                ),
                workspace=workspace,
                api_key=api_key,
                required_keys=("success", "changes"),
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
            if gate.get("status") != "passed":
                log(
                    f"native:{architecture}",
                    f"NEEDS_FIX target_contract round={repair_attempts}",
                )
                pending_review = {
                    "kind": "deterministic_target_contract",
                    "architecture": architecture,
                    "native_failure": native_failure,
                    "gate": gate,
                }
                continue
            log(
                f"native:{architecture}",
                f"PASS fixer round={repair_attempts}",
            )
            pending_review = None
            continue
