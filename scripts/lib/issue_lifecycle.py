"""Policies for terminal failure Issues and the explicit E2E Issue probe."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from scripts.lib.task_spec import TaskSpec
from scripts.utils.gitcode import GitCodeResource


class IssueLifecycleError(RuntimeError):
    """Raised when an Issue cannot be selected or updated safely."""


class IssueOperationForbiddenError(IssueLifecycleError):
    """Raised when the controlled E2E operation is invoked out of scope."""


_SAFE_DYNAMIC_VALUE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class FailureIssueReport:
    task: TaskSpec
    failure_stage: str
    attempt_count: int
    terminal_delivery_error: bool
    architecture_status: Mapping[str, str]
    repair_summaries: tuple[str, ...]
    run_url: str
    artifact_url: str
    suggested_action: str

    def __post_init__(self) -> None:
        if not 0 <= self.attempt_count <= 3:
            raise ValueError("attempt_count must be between 0 and 3")
        if not self.failure_stage.strip():
            raise ValueError("failure_stage is required")
        if set(self.architecture_status) != {"x86_64", "aarch64"}:
            raise ValueError("architecture_status must cover x86_64 and aarch64")
        for field_name in ("run_url", "artifact_url"):
            value = getattr(self, field_name)
            if not value.startswith("https://"):
                raise ValueError(f"{field_name} must be an HTTPS URL")


def _marker(idempotency_key: str) -> str:
    return f"<!-- oe-autopilot-task:{idempotency_key} -->"


def _default_title(report: FailureIssueReport) -> str:
    return (
        f"[Autopilot Failure] {report.task.app.capitalize()} "
        f"{report.task.version} on openEuler {report.task.os_version}"
    )


def _render_body(report: FailureIssueReport, idempotency_key: str) -> str:
    architecture_lines = "\n".join(
        f"- {arch}: {report.architecture_status[arch]}"
        for arch in ("x86_64", "aarch64")
    )
    repair_lines = "\n".join(
        f"- {summary}" for summary in report.repair_summaries
    )
    if not repair_lines:
        repair_lines = "- No automated repair was applicable."
    return "\n".join(
        (
            _marker(idempotency_key),
            "",
            "## Terminal automation failure",
            "",
            f"- Failure stage: {report.failure_stage}",
            f"- Repair attempts: {report.attempt_count}/3",
            f"- Workflow run: {report.run_url}",
            f"- Diagnostic artifact: {report.artifact_url}",
            "",
            "## TaskSpec",
            "",
            "```json",
            report.task.to_json(),
            "```",
            "",
            "## Native architecture status",
            "",
            architecture_lines,
            "",
            "## Automated repair summary",
            "",
            repair_lines,
            "",
            "## Suggested human action",
            "",
            report.suggested_action,
        )
    )


def _existing_issue(
    client: Any,
    *,
    target_repo: str,
    idempotency_key: str,
) -> Mapping[str, object] | None:
    marker = _marker(idempotency_key)
    issues = client.list_issues(
        target_repo=target_repo,
        state="all",
        search=idempotency_key,
    )
    matches = [
        issue
        for issue in issues
        if marker in str(issue.get("body", ""))
    ]
    if len(matches) > 1:
        raise IssueLifecycleError(
            f"multiple Issues contain idempotency marker {idempotency_key}"
        )
    return matches[0] if matches else None


def report_terminal_failure(
    *,
    client: Any,
    target_repo: str,
    report: FailureIssueReport,
    successful: bool,
    idempotency_key: str | None = None,
    title: str | None = None,
    create_if_missing: bool = True,
) -> GitCodeResource | Any | None:
    """Create or update one terminal failure Issue.

    Ordinary failures are silent until all three repair attempts have failed.
    A non-recoverable delivery error can report earlier.
    """

    if successful:
        return None
    if report.attempt_count < 3 and not report.terminal_delivery_error:
        return None

    key = idempotency_key or report.task.task_id
    issue_title = title or _default_title(report)
    body = _render_body(report, key)
    existing = _existing_issue(
        client,
        target_repo=target_repo,
        idempotency_key=key,
    )
    if existing is None:
        if not create_if_missing:
            raise IssueLifecycleError(
                "new Issue is not visible for the idempotent update; "
                "refusing to create a duplicate"
            )
        return client.create_issue(
            target_repo=target_repo,
            title=issue_title,
            body=body,
            labels="needs-human-review",
        )

    raw_number = existing.get("number")
    if raw_number is None:
        raw_number = existing.get("iid")
    try:
        number = int(raw_number)
    except (TypeError, ValueError) as error:
        raise IssueLifecycleError("existing Issue has no valid number") from error
    return client.update_issue(
        target_repo=target_repo,
        number=number,
        title=issue_title,
        body=body,
        state="open",
        labels="needs-human-review",
    )


def run_controlled_issue_probe(
    *,
    client: Any,
    target_repo: str,
    task: TaskSpec,
    environment: str,
    operation: str,
    github_run_id: str,
    failure_stage: str,
) -> GitCodeResource | Any:
    """Run the explicit test-only create/update/comment/close contract probe."""

    if environment != "test" or operation != "failure_issue_contract_test":
        raise IssueOperationForbiddenError(
            "controlled Issue probe requires the explicit test operation"
        )
    if not _SAFE_DYNAMIC_VALUE.fullmatch(github_run_id):
        raise ValueError("github_run_id contains unsafe characters")
    if not _SAFE_DYNAMIC_VALUE.fullmatch(failure_stage):
        raise ValueError("failure_stage contains unsafe characters")

    key = f"e2e-{task.task_id}-run-{github_run_id}"
    title = (
        f"[E2E TEST] {task.app.capitalize()} {task.version} "
        f"{failure_stage} final failure - run {github_run_id}"
    )
    run_url = (
        "https://github.com/Meihan-chen/openeuler-docker-images-workflow/"
        f"actions/runs/{github_run_id}"
    )
    report = FailureIssueReport(
        task=task,
        failure_stage=failure_stage,
        attempt_count=3,
        terminal_delivery_error=False,
        architecture_status={
            "x86_64": "passed (controlled probe)",
            "aarch64": "failed (controlled probe)",
        },
        repair_summaries=(
            "round 1: controlled retry recorded",
            "round 2: controlled retry recorded",
            "round 3: controlled terminal failure recorded",
        ),
        run_url=run_url,
        artifact_url=run_url,
        suggested_action="No human action is required for this E2E probe.",
    )

    created = report_terminal_failure(
        client=client,
        target_repo=target_repo,
        report=report,
        successful=False,
        idempotency_key=key,
        title=title,
    )
    updated = report_terminal_failure(
        client=client,
        target_repo=target_repo,
        report=report,
        successful=False,
        idempotency_key=key,
        title=title,
        create_if_missing=False,
    )
    if created is None or updated is None:
        raise IssueLifecycleError("controlled Issue probe did not create an Issue")

    client.create_issue_comment(
        target_repo=target_repo,
        number=updated.number,
        body=(
            "E2E contract verified: creation and idempotent update both "
            "succeeded. Closing this controlled probe."
        ),
    )
    closed_body = (
        f"{_render_body(report, key)}\n\n"
        "## E2E contract result\n\n"
        "The controlled Issue was closed automatically after create, update, "
        "comment and idempotency checks passed."
    )
    return client.update_issue(
        target_repo=target_repo,
        number=updated.number,
        title=title,
        body=closed_body,
        state="closed",
        labels="needs-human-review",
    )
