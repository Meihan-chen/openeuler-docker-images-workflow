"""Prepare application-level tests for one openEuler upgrade candidate."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from scripts.lib.agent_runtime import AgentResult, run_agent
from scripts.lib.generation_pipeline import build_role_prompt
from scripts.lib.oe_upgrade_sanitizer import (
    SanitizationReport,
    create_checkpoint,
    sanitize_agent_changes,
)
from scripts.lib.task_spec import TaskSpec
from scripts.lib.target_contract import TargetContractError, validate_test_contract


class UpgradeTestPreparationError(RuntimeError):
    """Raised when shared runtime tests cannot be prepared safely."""


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
        timeout=3600,
    )
    if creator.payload.get("success") is not True:
        raise UpgradeTestPreparationError("testcase_creator did not complete")
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

    # QA is read-only and advisory; it can report test defects but cannot
    # mutate the candidate or override deterministic/runtime validation.
    qa = agent_runner(
        executable=executable,
        role="testcase_qa",
        prompt=build_role_prompt(
            role="testcase_qa", task=task, base_sha=base_sha
        ),
        workspace=workspace,
        api_key=api_key,
        required_keys=("issues", "summary"),
        response_keys=("status", "issues", "coverage_score", "summary"),
        timeout=1200,
    )
    (evidence_dir / "testcase-qa-round1.json").write_text(
        json.dumps(qa.payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    return UpgradeTestPreparationResult(
        status="generated",
        reused_existing=False,
        creator_payload=creator.payload,
        qa_payload=qa.payload,
        sanitization=sanitization,
    )
