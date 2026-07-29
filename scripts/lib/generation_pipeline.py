"""Adversarial Agent generation stage followed by deterministic target gates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from scripts.harness.gate_diff import validate_generated_target
from scripts.lib.agent_runtime import AgentResult, run_agent
from scripts.lib.task_spec import TaskSpec


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


@dataclass(frozen=True)
class GenerationResult:
    status: str
    qa_fix_rounds: int
    gate_report: Mapping[str, object]


def _tag(task: TaskSpec) -> str:
    os_tag = task.os_version.replace(".", "").replace("-lts-sp", "sp")
    return f"{task.version}-oe{os_tag}"


def _application_contract(task: TaskSpec) -> tuple[str, ...]:
    if task.app != "kvrocks":
        return ()
    return (
        "- Kvrocks runtime contract: UID/GID 999, TCP 6666, writable "
        "`/var/lib/kvrocks`, Redis-protocol PING, restart persistence, "
        "configuration, LICENSE and NOTICE.",
        "- The native harness executes shared tests inside an already-running "
        "container; test scripts must not invoke Docker or own container "
        "lifecycle.",
    )


def build_role_prompt(
    *,
    role: str,
    task: TaskSpec,
    base_sha: str,
    review: Mapping[str, object] | None = None,
) -> str:
    try:
        instructions = (_PROMPT_DIR / _PROMPT_FILES[role]).read_text()
    except (KeyError, OSError) as error:
        raise GenerationPipelineError(f"prompt is unavailable for role {role}") from error
    app_root = f"{task.domain}/{task.app}"
    image_root = f"{app_root}/{task.version}/{task.os_version}"
    context = "\n".join(
        (
            "## Immutable task contract",
            "",
            f"- Target base SHA: `{base_sha}`",
            f"- TaskSpec: `{task.to_json()}`",
            f"- New MDU root: `{app_root}/`",
            f"- Dockerfile: `{image_root}/Dockerfile`",
            f"- Dockerfile test entrypoint: `{image_root}/test.sh`",
            f"- Shared tests: `{app_root}/tests/`",
            f"- Future result root: `{app_root}/results/{task.version}/{task.os_version}/`",
            f"- Meta tag: `{_tag(task)}`",
            f"- Source tag: `v{task.version}` from `{task.source_url}`",
            f"- Existing list allowed to change: `{task.domain}/image-list.yml`",
            "- Do not modify any other path.",
            "- Use the official `./x.py build` command with `-j 4`.",
            *_application_contract(task),
            "- Do not run git commit, git push, or any GitCode API write.",
        )
    )
    parts = [instructions.rstrip(), context]
    if review is not None:
        parts.extend(
            (
                "## QA report to resolve",
                "",
                "```json",
                json.dumps(review, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
            )
        )
    return "\n\n".join(parts) + "\n"


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


def _default_validator(
    *,
    workspace: Path,
    task: TaskSpec,
    base_sha: str,
) -> dict[str, object]:
    return validate_generated_target(repo=workspace, task=task, base_sha=base_sha)


def _run(
    *,
    agent_runner: Callable[..., AgentResult],
    executable: Path,
    workspace: Path,
    api_key: str,
    role: str,
    prompt: str,
) -> AgentResult:
    return agent_runner(
        executable=executable,
        role=role,
        prompt=prompt,
        workspace=workspace,
        api_key=api_key,
        required_keys=_REQUIRED_KEYS[role],
    )


def _review_pair(
    *,
    subject: str,
    qa_role: str,
    agent_runner: Callable[..., AgentResult],
    executable: Path,
    workspace: Path,
    report_dir: Path,
    task: TaskSpec,
    base_sha: str,
    api_key: str,
) -> int:
    review = _run(
        agent_runner=agent_runner,
        executable=executable,
        workspace=workspace,
        api_key=api_key,
        role=qa_role,
        prompt=build_role_prompt(role=qa_role, task=task, base_sha=base_sha),
    )
    _write_report(report_dir, f"{qa_role.replace('_', '-')}-round1.json", review.payload, api_key)
    if review.payload.get("status") == "approved":
        return 0
    if review.payload.get("status") != "needs_fix":
        raise GenerationPipelineError(f"{qa_role} returned an invalid status")

    fixed = _run(
        agent_runner=agent_runner,
        executable=executable,
        workspace=workspace,
        api_key=api_key,
        role="fixer",
        prompt=build_role_prompt(
            role="fixer",
            task=task,
            base_sha=base_sha,
            review=review.payload,
        ),
    )
    if fixed.payload.get("success") is not True:
        raise GenerationPipelineError(f"fixer failed for {subject}")
    _write_report(
        report_dir,
        f"fixer-{subject}-round1.json",
        fixed.payload,
        api_key,
    )

    second = _run(
        agent_runner=agent_runner,
        executable=executable,
        workspace=workspace,
        api_key=api_key,
        role=qa_role,
        prompt=build_role_prompt(role=qa_role, task=task, base_sha=base_sha),
    )
    _write_report(
        report_dir,
        f"{qa_role.replace('_', '-')}-round2.json",
        second.payload,
        api_key,
    )
    if second.payload.get("status") != "approved":
        raise GenerationPipelineError(
            f"{qa_role} did not approve after the scoped fix"
        )
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

    creator = _run(
        agent_runner=agent_runner,
        executable=executable,
        workspace=workspace,
        api_key=api_key,
        role="image_creator",
        prompt=build_role_prompt(
            role="image_creator",
            task=task,
            base_sha=base_sha,
        ),
    )
    if creator.payload.get("success") is not True:
        raise GenerationPipelineError("image_creator did not complete successfully")
    _write_report(report_dir, "image-creator.json", creator.payload, api_key)

    fix_rounds = _review_pair(
        subject="image",
        qa_role="image_qa",
        agent_runner=agent_runner,
        executable=executable,
        workspace=workspace,
        report_dir=report_dir,
        task=task,
        base_sha=base_sha,
        api_key=api_key,
    )

    testcase = _run(
        agent_runner=agent_runner,
        executable=executable,
        workspace=workspace,
        api_key=api_key,
        role="testcase_creator",
        prompt=build_role_prompt(
            role="testcase_creator",
            task=task,
            base_sha=base_sha,
        ),
    )
    if testcase.payload.get("success") is not True:
        raise GenerationPipelineError("testcase_creator did not complete successfully")
    _write_report(report_dir, "testcase-creator.json", testcase.payload, api_key)

    fix_rounds += _review_pair(
        subject="testcase",
        qa_role="testcase_qa",
        agent_runner=agent_runner,
        executable=executable,
        workspace=workspace,
        report_dir=report_dir,
        task=task,
        base_sha=base_sha,
        api_key=api_key,
    )

    gate_report = target_validator(
        workspace=workspace,
        task=task,
        base_sha=base_sha,
    )
    if gate_report.get("status") != "passed":
        raise GenerationPipelineError("deterministic target contract did not pass")
    _write_report(report_dir, "gates.json", gate_report, api_key)
    return GenerationResult(
        status="passed",
        qa_fix_rounds=fix_rounds,
        gate_report=gate_report,
    )
