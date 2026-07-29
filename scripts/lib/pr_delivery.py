"""PR evidence composition and push/create cleanup orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from scripts.lib.candidate_bundle import CandidateBundle
from scripts.lib.delivery_config import DeliveryConfig
from scripts.lib.git_delivery import delete_working_branch, push_working_branch


class PRDeliveryError(RuntimeError):
    """Raised when delivery policy refuses or cleanup cannot restore safety."""


@dataclass(frozen=True)
class PullRequestContent:
    title: str
    body: str


def _load_json(path: Path, label: str) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise PRDeliveryError(f"{label} evidence is invalid") from error
    if not isinstance(payload, dict):
        raise PRDeliveryError(f"{label} evidence must be an object")
    return payload


def _last_qa_status(root: Path, prefix: str) -> str:
    paths = sorted((root / "reports" / "agents").glob(f"{prefix}-round*.json"))
    if not paths:
        return "not recorded"
    report = _load_json(paths[-1], prefix)
    status = str(report.get("status", "unknown"))
    if status != "approved":
        raise PRDeliveryError(f"{prefix} evidence is not approved")
    return status


def compose_pull_request(bundle: CandidateBundle) -> PullRequestContent:
    task = bundle.task
    rows = []
    for architecture in ("x86_64", "aarch64"):
        report = _load_json(
            bundle.root / "reports" / f"{architecture}.json",
            architecture,
        )
        if report.get("status") != "passed":
            raise PRDeliveryError(f"{architecture} evidence did not pass")
        checks = report.get("checks")
        if not isinstance(checks, dict) or not all(
            value is True for value in checks.values()
        ):
            raise PRDeliveryError(f"{architecture} checks are incomplete")
        rows.append(
            "| "
            + " | ".join(
                (
                    architecture,
                    str(report.get("platform", "")),
                    str(report.get("image_id", "")),
                    f"{report.get('duration_seconds', '')}s",
                    ", ".join(sorted(checks)),
                )
            )
            + " |"
        )
    gates = _load_json(bundle.root / "reports" / "gates.json", "target gates")
    if gates.get("status") != "passed":
        raise PRDeliveryError("deterministic target gates did not pass")
    image_qa = _last_qa_status(bundle.root, "image-qa")
    testcase_qa = _last_qa_status(bundle.root, "testcase-qa")
    title = (
        f"[New Image] Add Apache {task.app.capitalize()} {task.version} "
        f"for openEuler {task.os_version}"
    )
    body = "\n".join(
        (
            "## Summary",
            "",
            f"Add Apache {task.app.capitalize()} {task.version} built from "
            f"[the upstream release]({task.source_url}) on openEuler "
            f"{task.os_version}.",
            "",
            "The runtime is built and tested natively on both supported "
            "architectures. It runs as a non-root user and includes a "
            "Redis-protocol health check plus restart-persistence coverage.",
            "",
            "## Validated candidate",
            "",
            f"- Promoted from validated run `{bundle.manifest.validated_run_id}`.",
            f"- Target base SHA: `{bundle.manifest.base_sha}`.",
            f"- Candidate SHA256: `{bundle.manifest.content_sha256}`.",
            f"- Task ID: `{bundle.manifest.task_id}`.",
            "",
            "## Native build and test evidence",
            "",
            "| Architecture | Platform | Image ID | Duration | Passed checks |",
            "|---|---|---|---:|---|",
            *rows,
            "",
            "Each architecture passed native source build, dgoss assertions, "
            "shared version/function tests, and restart-persistence validation.",
            "",
            "## Adversarial review",
            "",
            f"- image QA: {image_qa}",
            f"- testcase QA: {testcase_qa}",
            "",
            "## Repository checks",
            "",
            f"- Deterministic target contract: {gates.get('status')}",
            f"- Added files: {gates.get('added_files', 'recorded in artifact')}",
            "- Existing image files were not modified or deleted.",
            "- `Database/image-list.yml` preserves all prior entries.",
            "- Application tests and bounded result evidence are included under "
            "the application MDU.",
            "",
            "## Checklist",
            "",
            "- [x] Version and upstream source are pinned",
            "- [x] openEuler base and metadata paths are consistent",
            "- [x] x86_64 native build and tests passed",
            "- [x] aarch64 native build and tests passed",
            "- [x] Non-root runtime, health check, and persistence passed",
            "- [x] Candidate integrity verified before delivery",
        )
    )
    return PullRequestContent(title=title, body=body + "\n")


def deliver_promoted_candidate(
    *,
    repo: Path,
    config: DeliveryConfig,
    promotion: Any,
    username: str,
    token: str,
    title: str,
    body: str,
    client: Any,
    push: Callable[..., None] = push_working_branch,
    delete: Callable[..., bool] = delete_working_branch,
) -> Any:
    if not config.allows_branch_push or not config.allows_pr_create:
        raise PRDeliveryError(
            f"delivery mode {config.delivery_mode} forbids PR delivery writes"
        )
    push(
        repo=repo,
        config=config,
        branch=promotion.branch,
        username=username,
        token=token,
    )
    try:
        return client.create_pull_request(
            config=config,
            title=title,
            body=body,
            branch=promotion.branch,
        )
    except Exception:
        try:
            delete(
                repo=repo,
                config=config,
                branch=promotion.branch,
                username=username,
                token=token,
            )
        except Exception as cleanup_error:
            raise PRDeliveryError(
                "PR creation failed and exact working branch cleanup also failed"
            ) from cleanup_error
        raise
