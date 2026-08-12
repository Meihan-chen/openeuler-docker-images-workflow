"""One-shot controller for a serial openEuler upgrade activity."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from scripts.lib.oe_upgrade_activity import (
    ActivityError,
    ResolvedTaskState,
    ensure_issue_comment,
    establish_request,
    failure_marker,
    parse_failure_reasons,
    parse_request_comment,
    planning_failure_marker,
    render_failure_comment,
    render_planning_failure_comment,
    render_summary_comment,
    state_digest,
    summary_marker,
)
from scripts.lib.oe_upgrade_advance import (
    PullRequestView,
    RunView,
    resolve_advance,
    target_state_index,
)
from scripts.lib.oe_upgrade_contract import (
    UpgradeRequest,
    normalize_oe_version,
    normalize_scope,
    parse_upgrade_issue,
)
from scripts.lib.oe_upgrade_planner import UpgradePlan, plan_upgrade
from scripts.lib.task_spec import TaskSpec


class UpgradeControllerError(RuntimeError):
    """Raised when entry data or external activity state is inconsistent."""


Dispatch = Callable[..., None]


@dataclass(frozen=True)
class ActivityResult:
    action: str
    request: UpgradeRequest
    plan: UpgradePlan
    states: tuple[ResolvedTaskState, ...]
    next_task: TaskSpec | None


@dataclass(frozen=True)
class WorkerStartResult:
    proceed: bool
    reason: str


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if completed.returncode != 0:
        raise UpgradeControllerError(
            completed.stderr.strip() or f"git {' '.join(args)} failed"
        )
    return completed.stdout.strip()


def _pull_views(raw: Sequence[Mapping[str, object]]) -> tuple[PullRequestView, ...]:
    views: list[PullRequestView] = []
    for item in raw:
        number = item.get("number")
        if number is None:
            continue
        views.append(
            PullRequestView(
                number=int(number),
                url=str(item.get("url", "")),
                title=str(item.get("title", "")),
                body=str(item.get("body", "")),
                head=str(item.get("head", "")),
                base=str(item.get("base", "")),
                state=str(item.get("state", "")),
                merged=bool(item.get("merged")),
            )
        )
    return tuple(views)


def _plan_preview_body(
    *, request: UpgradeRequest, plan: UpgradePlan, run_url: str, artifact_name: str
) -> str:
    return "\n".join(
        (
            "openEuler 升级规划预览已生成；本次没有建立交付活动。",
            "",
            f"- Target openEuler: `{request.oe_version}`",
            f"- Scope: `{', '.join(request.scope)}`",
            f"- Base SHA: `{request.base_sha}`",
            f"- Indexed MDU: `{plan.summary['mdu_count']}`",
            f"- Tasks: `{plan.summary['task_count']}`",
            f"- Planning failures: `{plan.summary['planning_failed_count']}`",
            f"- Warnings: `{plan.summary['warning_count']}`",
            f"- Workflow: {run_url}",
            f"- Plan artifact: `{artifact_name}`",
        )
    )


def _update_issue_status(
    *, client: Any, target_repo: str, issue: Mapping[str, object], status: str
) -> None:
    client.update_issue(
        target_repo=target_repo,
        number=int(issue.get("number", issue.get("iid", 0))),
        title=str(issue.get("title", "")),
        body=str(issue.get("body", "") or ""),
        state="open",
        issue_status=status,
    )


def _validate_expected(
    *, issue: Mapping[str, object], expected_oe_version: str, expected_scope: object,
    mode: str,
):
    options = parse_upgrade_issue(
        int(issue.get("number", issue.get("iid", 0))),
        str(issue.get("title", "")),
        str(issue.get("body", "") or ""),
        mode=mode,
    )
    if options.oe_version != normalize_oe_version(expected_oe_version):
        raise UpgradeControllerError("workflow oe_version does not match Issue")
    if options.scope != normalize_scope(expected_scope):
        raise UpgradeControllerError("workflow scope does not match Issue")
    return options


def verify_worker_start(
    *,
    client: Any,
    target_repo: str,
    issue_number: int,
    request_key: str,
    task: TaskSpec,
    workspace: Path,
    trusted_author: str,
    head_revision: str,
) -> WorkerStartResult:
    comments = client.list_issue_comments(
        target_repo=target_repo, number=issue_number
    )
    request = parse_request_comment(comments, trusted_author=trusted_author)
    if (
        request.request_key != request_key
        or request.tracking_issue_number != issue_number
        or request.oe_version != task.os_version
    ):
        raise UpgradeControllerError("worker inputs do not match UpgradeRequest")
    matching = [
        candidate
        for candidate in plan_upgrade(workspace, request).tasks
        if candidate.task_key == task.task_key
    ]
    if matching != [task]:
        raise UpgradeControllerError("worker TaskSpec does not match pinned plan")
    target = target_state_index(workspace, head_revision, (task,))[task.task_key]
    if target == "exists":
        return WorkerStartResult(False, "target-exists")
    if target == "inconsistent":
        return WorkerStartResult(False, "target-inconsistent")
    failure_reasons = parse_failure_reasons(
        comments,
        request_key=request.request_key,
        trusted_author=trusted_author,
    )
    if task.task_key in failure_reasons:
        return WorkerStartResult(False, "failure-marker")
    pulls = _pull_views(
        client.list_pull_requests(
            target_repo=target_repo, state="all", base="master"
        )
    )
    matching_pulls = [
        pull
        for pull in pulls
        if pull.head == task.branch and f"oe-upgrade-task:{task.task_key}" in pull.body
    ]
    if matching_pulls:
        return WorkerStartResult(False, "open-pr")
    return WorkerStartResult(True, "")


def run_activity(
    *,
    client: Any,
    target_repo: str,
    issue: Mapping[str, object],
    workspace: Path,
    mode: str,
    expected_oe_version: str,
    expected_scope: object,
    trusted_author: str,
    run_url: str,
    artifact_name: str,
    runs: Sequence[RunView],
    dispatch: Dispatch,
) -> ActivityResult:
    """Plan, project state, and perform at most one serial scheduling write."""
    options = _validate_expected(
        issue=issue,
        expected_oe_version=expected_oe_version,
        expected_scope=expected_scope,
        mode=mode,
    )
    comments = client.list_issue_comments(
        target_repo=target_repo,
        number=options.tracking_issue_number,
    )
    if mode == "deliver":
        try:
            request = parse_request_comment(
                comments, trusted_author=trusted_author
            )
        except ActivityError as error:
            if str(error) != "no trusted upgrade request comment exists":
                raise
            request = UpgradeRequest.create(
                tracking_issue_number=options.tracking_issue_number,
                oe_version=options.oe_version,
                scope=options.scope,
                base_sha=_git(workspace, "rev-parse", "HEAD"),
            )
            establish_request(
                client=client,
                target_repo=target_repo,
                issue=issue,
                request=request,
                mode=mode,
                trusted_author=trusted_author,
            )
            comments = client.list_issue_comments(
                target_repo=target_repo,
                number=options.tracking_issue_number,
            )
    else:
        request = UpgradeRequest.create(
            tracking_issue_number=options.tracking_issue_number,
            oe_version=options.oe_version,
            scope=options.scope,
            base_sha=_git(workspace, "rev-parse", "HEAD"),
        )
    plan = plan_upgrade(workspace, request)

    if mode == "plan":
        client.create_issue_comment(
            target_repo=target_repo,
            number=request.tracking_issue_number,
            body=_plan_preview_body(
                request=request,
                plan=plan,
                run_url=run_url,
                artifact_name=artifact_name,
            ),
        )
        return ActivityResult("planned", request, plan, (), None)

    for failure in plan.planning_failures:
        marker = planning_failure_marker(
            request.request_key,
            failure["mdu_path"],
            request.oe_version,
        )
        ensure_issue_comment(
            client=client,
            target_repo=target_repo,
            issue_number=request.tracking_issue_number,
            body=render_planning_failure_comment(
                request=request, failure=failure
            ),
            marker=marker,
            trusted_author=trusted_author,
        )
    comments = client.list_issue_comments(
        target_repo=target_repo,
        number=request.tracking_issue_number,
    )
    failure_reasons = parse_failure_reasons(
        comments,
        request_key=request.request_key,
        trusted_author=trusted_author,
    )
    pull_requests = _pull_views(
        client.list_pull_requests(
            target_repo=target_repo,
            state="all",
            base="master",
        )
    )
    base_targets = target_state_index(workspace, request.base_sha, plan.tasks)
    head_sha = _git(workspace, "rev-parse", "HEAD")
    head_targets = target_state_index(workspace, head_sha, plan.tasks)
    projection = resolve_advance(
        request=request,
        tasks=plan.tasks,
        base_targets=base_targets,
        head_targets=head_targets,
        pull_requests=pull_requests,
        failure_reasons=failure_reasons,
        runs=runs,
    )

    if projection.fallback_failures:
        tasks = {task.task_key: task for task in plan.tasks}
        runs_by_task = {run.task_key: run for run in runs}
        for task_key, reason in projection.fallback_failures:
            task = tasks[task_key]
            run = runs_by_task.get(task_key)
            ensure_issue_comment(
                client=client,
                target_repo=target_repo,
                issue_number=request.tracking_issue_number,
                body=render_failure_comment(
                    request=request,
                    task=task,
                    reason=reason,
                    run_url=run.url if run else run_url,
                    summary="Activity state reconciliation detected a terminal failure.",
                    artifact_name=(
                        f"oe-upgrade-worker-{run.run_id}"
                        if run
                        else artifact_name
                    ),
                ),
                marker=failure_marker(request.request_key, task_key),
                trusted_author=trusted_author,
            )
        comments = client.list_issue_comments(
            target_repo=target_repo,
            number=request.tracking_issue_number,
        )
        projection = resolve_advance(
            request=request,
            tasks=plan.tasks,
            base_targets=base_targets,
            head_targets=head_targets,
            pull_requests=pull_requests,
            failure_reasons=parse_failure_reasons(
                comments,
                request_key=request.request_key,
                trusted_author=trusted_author,
            ),
            runs=runs,
        )

    if projection.action == "active-worker-exists":
        return ActivityResult("active", request, plan, projection.states, None)
    if projection.next_task is not None:
        dispatch(
            task=projection.next_task,
            request=request,
            issue_number=request.tracking_issue_number,
        )
        return ActivityResult(
            "dispatched", request, plan, projection.states, projection.next_task
        )

    digest = state_digest(projection.states, plan.planning_failures)
    marker = summary_marker(request.request_key, digest)
    ensure_issue_comment(
        client=client,
        target_repo=target_repo,
        issue_number=request.tracking_issue_number,
        body=render_summary_comment(
            request=request,
            states=projection.states,
            planning_failures=plan.planning_failures,
            run_url=run_url,
            artifact_name=artifact_name,
        ),
        marker=marker,
        trusted_author=trusted_author,
    )
    _update_issue_status(
        client=client,
        target_repo=target_repo,
        issue=issue,
        status="已挂起",
    )
    return ActivityResult("finalized", request, plan, projection.states, None)
