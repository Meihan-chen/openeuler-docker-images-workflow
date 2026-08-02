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


@dataclass(frozen=True)
class RoundDecision:
    converged: bool
    round_number: int
    repair_attempts: int
    validated_patch_sha256: str


_ARCHITECTURES = ("x86_64", "aarch64")
# The gate is deterministic and free; a Docker round is neither. When the
# Fixer's edit violates the target contract, re-asking it here is far cheaper
# than spending another dual-architecture build to discover the same thing.
_GATE_REPAIR_ATTEMPTS = 3


def _passed(report: Mapping[str, object]) -> bool:
    return report.get("status") == "passed"


def _native_gate_ready(gate: Mapping[str, object]) -> bool:
    """Require safe build input and executable tests before paying for Docker."""
    return (
        gate.get("status") == "passed"
        and gate.get("build_allowed", True) is True
        and gate.get("test_allowed", True) is True
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
    return {
        **review,
        "classification": classify_failure(
            report=report,
            gate=gate,
            allowed_roots=(task.domain,),
        ),
    }


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
    agent_runner: Callable[..., AgentResult] = run_agent,
    target_validator: Callable[..., Mapping[str, object]] = (
        validate_generated_target
    ),
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
    missing = [name for name in _ARCHITECTURES if name not in reports]
    if missing:
        raise NativeRepairError(
            "round decision needs both architectures: missing "
            + ", ".join(missing)
        )
    report_dir.mkdir(parents=True, exist_ok=True)
    stage = f"round:{round_number}"

    if all(_passed(reports[name]) for name in _ARCHITECTURES):
        digests = {
            str(reports[name].get("validated_patch_sha256", ""))
            for name in _ARCHITECTURES
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
                reports["x86_64"]["validated_patch_sha256"]
            ),
        )

    if round_number > max_rounds:
        failed = [
            name for name in _ARCHITECTURES if not _passed(reports[name])
        ]
        raise NativeRepairError(
            f"native validation failed on {', '.join(failed)} after "
            f"{max_rounds} repair attempts"
        )

    if not api_key:
        # Converging needs no model, so the key is only required once a repair
        # is actually about to run.
        raise NativeRepairError("DEEPSEEK_API_KEY is required to repair")
    # One Fixer sees both architectures, so a fix for one cannot silently
    # regress the other — and for the same reason one category cannot speak
    # for both. A Goss config fault on x86 next to a build failure on ARM must
    # not be handed over as "do not change the Dockerfile".
    per_architecture = {
        name: classify_failure(
            report=reports[name],
            allowed_roots=(task.domain,),
        )
        for name in _ARCHITECTURES
        if not _passed(reports[name])
    }
    if all(
        result["category"] == "infra" for result in per_architecture.values()
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
            for name in _ARCHITECTURES
        },
    }
    for attempt in range(1, _GATE_REPAIR_ATTEMPTS + 1):
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
        if fixed.payload.get("success") is not True:
            raise NativeRepairError(
                f"fixer failed in round {round_number} attempt {attempt}"
            )
        gate = _target_gate_report(
            target_validator=target_validator,
            workspace=workspace,
            task=task,
            base_sha=base_sha,
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

    raise NativeRepairError(
        f"repaired candidate still fails the target contract in round "
        f"{round_number}"
    )


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
                    workspace=workspace,
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
