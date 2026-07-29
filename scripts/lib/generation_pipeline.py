"""Adversarial Agent generation stage followed by deterministic target gates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import yaml

from scripts.harness.gate_diff import validate_generated_target
from scripts.lib.agent_runtime import AgentResult, run_agent
from scripts.lib.progress import log
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
                "Only fix the reported QA issues; do not regenerate unrelated content.",
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
    creator_role: str,
    qa_role: str,
    agent_runner: Callable[..., AgentResult],
    executable: Path,
    workspace: Path,
    report_dir: Path,
    task: TaskSpec,
    base_sha: str,
    api_key: str,
) -> int:
    log("review", f"START {qa_role} round=1")
    review = _run(
        agent_runner=agent_runner,
        executable=executable,
        workspace=workspace,
        api_key=api_key,
        role=qa_role,
        prompt=build_role_prompt(role=qa_role, task=task, base_sha=base_sha),
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
        prompt=build_role_prompt(
            role=creator_role,
            task=task,
            base_sha=base_sha,
            review=review.payload,
        ),
    )
    if fixed.payload.get("success") is not True:
        raise GenerationPipelineError(f"{creator_role} repair failed")
    log("repair", f"PASS {creator_role} round=2")
    _write_report(
        report_dir,
        f"{creator_role.replace('_', '-')}-round2.json",
        fixed.payload,
        api_key,
    )

    log("review", f"START {qa_role} round=2")
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

    log("generate", "START image_creator")
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
    log("generate", "PASS image_creator")
    _write_report(report_dir, "image-creator.json", creator.payload, api_key)

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
    )

    log("generate", "START testcase_creator")
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
    log("generate", "PASS testcase_creator")
    _write_report(report_dir, "testcase-creator.json", testcase.payload, api_key)

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
    )

    log("gate", "START target_contract")
    gate_report = target_validator(
        workspace=workspace,
        task=task,
        base_sha=base_sha,
    )
    _write_report(report_dir, "gates.json", gate_report, api_key)
    if gate_report.get("status") != "passed":
        raise GenerationPipelineError("deterministic target contract did not pass")
    log("gate", "PASS target_contract")
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
        "RUN groupadd --gid 999 kvrocks && "
        "useradd --uid 999 --gid kvrocks kvrocks && "
        "mkdir -p /var/lib/kvrocks && "
        "chown -R 999:999 /var/lib/kvrocks\n"
        "COPY --from=builder /src/kvrocks/build/kvrocks "
        "/usr/local/bin/kvrocks\n"
        "USER 999\n"
        "EXPOSE 6666\n"
        "HEALTHCHECK CMD redis-cli -p 6666 PING | grep PONG\n"
        'ENTRYPOINT ["kvrocks", "--bind", "0.0.0.0"]\n'
    )
    entry = image / "test.sh"
    entry.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        f"export EXPECTED_VERSION={task.version}\n"
        'exec ../../tests/test.sh "$@"\n'
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
    for script in (entry, helpers, shared):
        script.chmod(0o755)

    log("smoke", "PASS deterministic candidate")
    return {"status": "passed", "mode": "pipeline_smoke"}
