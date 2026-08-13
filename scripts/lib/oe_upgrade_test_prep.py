"""Prepare application-level tests for one openEuler upgrade candidate."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from scripts.lib.agent_runtime import (
    AgentResult,
    AgentRuntimeError,
    run_agent,
    validate_agent_payload,
)
from scripts.lib.generation_pipeline import (
    build_role_prompt,
    review_testcase_candidate,
)
from scripts.lib.oe_upgrade_sanitizer import (
    SanitizationReport,
    create_checkpoint,
    sanitize_agent_changes,
)
from scripts.lib.task_spec import TaskSpec
from scripts.lib.target_contract import TargetContractError, validate_test_contract


class UpgradeTestPreparationError(RuntimeError):
    """Raised when shared runtime tests cannot be prepared safely."""


_TESTCASE_CREATOR_TIMEOUT_SECONDS = 7200


@dataclass(frozen=True)
class UpgradeTestPreparationResult:
    status: str
    reused_existing: bool
    creator_payload: dict[str, object] | None = None
    qa_payload: dict[str, object] | None = None
    sanitization: SanitizationReport | None = None


def prepare_upgrade_tests(
    *,
    workspace: Path,
    task: TaskSpec,
    base_sha: str,
    checkpoint_dir: Path,
    report_path: Path,
    evidence_dir: Path | None = None,
    executable: Path | None = None,
    api_key: str = "",
    agent_runner: Callable[..., AgentResult] = run_agent,
) -> UpgradeTestPreparationResult:
    evidence_dir = Path(evidence_dir or report_path.parent)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    if not task.mdu_path:
        raise UpgradeTestPreparationError("TaskSpec has no mdu_path")
    tests = workspace / task.mdu_path / "tests"
    entrypoint = tests / "test.sh"
    if entrypoint.is_file():
        contract = validate_test_contract(repo=workspace, task=task)
        if contract["runtime_test_allowed"] is not True:
            raise UpgradeTestPreparationError(
                "existing shared test.sh does not satisfy the runtime contract"
            )
        qa_payload: dict[str, object] = {
            "status": "approved",
            "issues": [],
            "coverage_score": 1.0,
            "summary": "Existing shared test contract was reused unchanged.",
        }
        (evidence_dir / "testcase-qa-round1.json").write_text(
            json.dumps(qa_payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
        return UpgradeTestPreparationResult(
            status="reused-existing",
            reused_existing=True,
            qa_payload=qa_payload,
        )
    if executable is None or not api_key:
        raise UpgradeTestPreparationError(
            "OpenCode executable and DEEPSEEK_API_KEY are required when tests are missing"
        )
    checkpoint = create_checkpoint(
        workspace=workspace,
        base_sha=base_sha,
        task=task,
        destination=checkpoint_dir,
        round_number=0,
        agent_role="testcase-creator",
    )
    creator = agent_runner(
        executable=executable,
        role="testcase_creator",
        prompt=build_role_prompt(
            role="testcase_creator", task=task, base_sha=base_sha
        ),
        workspace=workspace,
        api_key=api_key,
        required_keys=("success", "files_created"),
        response_keys=("success", "files_created", "command_evidence"),
        timeout=_TESTCASE_CREATOR_TIMEOUT_SECONDS,
    )
    if creator.payload.get("success") is not True:
        raise UpgradeTestPreparationError("testcase_creator did not complete")
    try:
        validate_agent_payload(
            creator.payload,
            required_keys=("success", "files_created", "command_evidence"),
        )
    except AgentRuntimeError as error:
        raise UpgradeTestPreparationError(
            f"testcase_creator command_evidence is invalid: {error}"
        ) from error
    (evidence_dir / "testcase-creator.json").write_text(
        json.dumps(
            creator.payload, ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n"
    )
    sanitization = sanitize_agent_changes(
        workspace=workspace,
        base_sha=base_sha,
        task=task,
        checkpoint=checkpoint,
        report_path=report_path,
    )
    contract = validate_test_contract(repo=workspace, task=task)
    if contract["runtime_test_allowed"] is not True:
        raise UpgradeTestPreparationError("generated shared tests did not pass gates")

    repair_checkpoint = create_checkpoint(
        workspace=workspace,
        base_sha=base_sha,
        task=task,
        destination=checkpoint_dir.with_name(checkpoint_dir.name + "-qa-repair"),
        round_number=0,
        agent_role="testcase-creator",
    )
    repaired_sanitization: SanitizationReport | None = None

    def post_repair_check(payload: Mapping[str, object]) -> None:
        nonlocal repaired_sanitization
        try:
            validate_agent_payload(
                payload,
                required_keys=("success", "files_created", "command_evidence"),
            )
        except AgentRuntimeError as error:
            raise UpgradeTestPreparationError(
                f"repaired testcase_creator command_evidence is invalid: {error}"
            ) from error
        repaired_sanitization = sanitize_agent_changes(
            workspace=workspace,
            base_sha=base_sha,
            task=task,
            checkpoint=repair_checkpoint,
            report_path=evidence_dir / "testcase-sanitization-round2.json",
        )
        repaired_contract = validate_test_contract(repo=workspace, task=task)
        if repaired_contract["runtime_test_allowed"] is not True:
            raise UpgradeTestPreparationError(
                "repaired shared tests did not pass gates"
            )

    review = review_testcase_candidate(
        agent_runner=agent_runner,
        executable=executable,
        workspace=workspace,
        report_dir=evidence_dir,
        task=task,
        base_sha=base_sha,
        api_key=api_key,
        creator_payload=creator.payload,
        post_repair_check=post_repair_check,
    )
    qa_path = evidence_dir / (
        "testcase-qa-round2.json"
        if (evidence_dir / "testcase-qa-round2.json").is_file()
        else "testcase-qa-round1.json"
    )
    qa_payload = json.loads(qa_path.read_text())
    return UpgradeTestPreparationResult(
        status="generated",
        reused_existing=False,
        creator_payload=dict(review.creator_payload or creator.payload),
        qa_payload=qa_payload,
        sanitization=repaired_sanitization or sanitization,
    )
