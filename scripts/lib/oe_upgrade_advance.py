"""Pure state projection for the serial openEuler upgrade scheduler."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import yaml

from scripts.lib.oe_upgrade_activity import ResolvedTaskState, task_marker
from scripts.lib.oe_upgrade_contract import UpgradeRequest
from scripts.lib.task_spec import TaskSpec


@dataclass(frozen=True)
class PullRequestView:
    number: int
    url: str
    title: str
    body: str
    head: str
    base: str
    state: str
    merged: bool


@dataclass(frozen=True)
class RunView:
    run_id: str
    task_key: str
    status: str
    conclusion: str
    url: str


@dataclass(frozen=True)
class AdvanceProjection:
    action: str
    states: tuple[ResolvedTaskState, ...]
    next_task: TaskSpec | None
    fallback_failures: tuple[tuple[str, str], ...]


_RUN_NAME_RE = re.compile(
    r"^oe-upgrade / (?P<request>[0-9a-f]{16}) / "
    r"(?P<task>[0-9a-f]{16}) / (?P<mdu>.+)$"
)


def parse_workflow_runs(
    raw_runs: Sequence[Mapping[str, object]], *, request_key: str
) -> tuple[RunView, ...]:
    runs: list[RunView] = []
    for raw in raw_runs:
        match = _RUN_NAME_RE.fullmatch(str(raw.get("displayTitle", "")))
        if not match or match.group("request") != request_key:
            continue
        run_id = str(raw.get("databaseId", ""))
        url = str(raw.get("url", ""))
        if not run_id.isdigit() or not url.startswith("https://"):
            raise ValueError("GitHub workflow run response is invalid")
        runs.append(
            RunView(
                run_id=run_id,
                task_key=match.group("task"),
                status=str(raw.get("status", "")),
                conclusion=str(raw.get("conclusion") or ""),
                url=url,
            )
        )
    return tuple(runs)


def list_github_workflow_runs(
    *,
    github_token: str,
    github_repository: str,
    workflow: str,
    run=subprocess.run,
) -> list[Mapping[str, object]]:
    if not github_token:
        raise ValueError("GITHUB_TOKEN is required")
    command = [
        "gh",
        "run",
        "list",
        "--repo",
        github_repository,
        "--workflow",
        workflow,
        "--limit",
        "1000",
        "--json",
        "databaseId,displayTitle,status,conclusion,url",
    ]
    environment = os.environ.copy()
    environment["GH_TOKEN"] = github_token
    completed = run(
        command,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    payload = json.loads(completed.stdout)
    if not isinstance(payload, list) or any(
        not isinstance(item, Mapping) for item in payload
    ):
        raise ValueError("GitHub workflow run list must be an array")
    return payload


def _git(repo: Path, *args: str, binary: bool = False) -> str | bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=not binary,
    )
    if completed.returncode != 0:
        detail = completed.stderr
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        raise ValueError(detail.strip() or f"git {' '.join(args)} failed")
    return completed.stdout


def _git_file(repo: Path, revision: str, relative: str) -> bytes | None:
    completed = subprocess.run(
        ["git", "show", f"{revision}:{relative}"],
        cwd=repo,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout


def _target_tag(task: TaskSpec) -> str:
    suffix = task.os_version.replace("-lts-sp", "sp").replace("-lts", "lts")
    return f"{task.version}-oe{suffix.replace('.', '').replace('-', '')}"


def target_state_index(
    repo: Path, revision: str, tasks: Sequence[TaskSpec]
) -> dict[str, str]:
    """Classify target combinations at one revision with two Git reads/task.

    Callers normally pass a local clone and run this for base and HEAD. Git
    remote traffic is therefore bounded to the clone/fetch, not multiplied by
    the number of TaskSpecs.
    """
    repo = Path(repo)
    _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}")
    states: dict[str, str] = {}
    for task in tasks:
        assert task.task_key and task.mdu_path
        dockerfile = (
            f"{task.mdu_path}/{task.version}/{task.os_version}/Dockerfile"
        )
        docker_exists = _git_file(repo, revision, dockerfile) is not None
        meta_bytes = _git_file(repo, revision, f"{task.mdu_path}/meta.yml")
        expected_path = f"{task.version}/{task.os_version}/Dockerfile"
        expected_entry: dict[str, object] = {"path": expected_path}
        if len(task.architectures) == 1:
            expected_entry["arch"] = task.architectures[0]
        meta_matches = False
        if meta_bytes is not None:
            try:
                meta = yaml.safe_load(meta_bytes) or {}
            except yaml.YAMLError:
                meta = None
            if isinstance(meta, Mapping):
                meta_matches = meta.get(_target_tag(task)) == expected_entry
                conflicting_paths = [
                    value
                    for key, value in meta.items()
                    if key != _target_tag(task)
                    and isinstance(value, Mapping)
                    and value.get("path") == expected_path
                ]
                if conflicting_paths:
                    meta_matches = False
        if docker_exists and meta_matches:
            states[task.task_key] = "exists"
        elif docker_exists or meta_matches:
            states[task.task_key] = "inconsistent"
        else:
            states[task.task_key] = "missing"
    return states


def _matching_pr(
    task: TaskSpec, pull_requests: Sequence[PullRequestView]
) -> PullRequestView | None:
    marker = task_marker(task.task_key or "")
    candidates = [
        pull
        for pull in pull_requests
        if pull.head == task.branch and marker in pull.body
    ]
    if len(candidates) > 1:
        raise ValueError(f"multiple pull requests match task {task.task_key}")
    return candidates[0] if candidates else None


def _state(
    *,
    request: UpgradeRequest,
    task: TaskSpec,
    base_target: str,
    head_target: str,
    pull_request: PullRequestView | None,
    failure_reason: str,
    runs: Sequence[RunView],
) -> tuple[ResolvedTaskState, tuple[str, str] | None]:
    task_runs = [run for run in runs if run.task_key == task.task_key]
    active = next(
        (run for run in task_runs if run.status in {"queued", "in_progress"}),
        None,
    )
    completed = next(
        (run for run in reversed(task_runs) if run.status == "completed"),
        None,
    )
    status = "pending"
    reason = ""
    evidence = "none"
    pr_number = None
    run_id = active.run_id if active else completed.run_id if completed else None
    fallback = None

    if base_target == "inconsistent" or head_target == "inconsistent":
        status, reason, evidence = "failed", "contract", "target-contract"
        fallback = (task.task_key or "", reason)
    elif base_target == "exists":
        status, evidence = "skipped-existing", "base-sha"
    elif head_target == "exists" and pull_request and pull_request.merged:
        status, evidence = "merged", "merged-pr"
        pr_number = pull_request.number
    elif head_target == "exists":
        status, evidence = "satisfied-after-base", "target-head"
    elif pull_request and pull_request.state in {"open", "opened"}:
        status, evidence = "pr-created", "open-pr"
        pr_number = pull_request.number
    elif failure_reason:
        status, reason, evidence = "failed", failure_reason, "failure-marker"
    elif active:
        status, evidence = "running", "active-run"
    elif completed:
        reason = (
            "infrastructure"
            if completed.conclusion in {"cancelled", "timed_out", "failure"}
            else "result-missing"
        )
        status, evidence = "failed", "completed-run-fallback"
        fallback = (task.task_key or "", reason)

    return (
        ResolvedTaskState(
            schema_version=1,
            request_key=request.request_key,
            task_key=task.task_key or "",
            mdu_path=task.mdu_path or "",
            status=status,
            reason=reason,
            evidence_source=evidence,
            run_id=run_id,
            pr_number=pr_number,
        ),
        fallback,
    )


def resolve_advance(
    *,
    request: UpgradeRequest,
    tasks: Sequence[TaskSpec],
    base_targets: Mapping[str, str],
    head_targets: Mapping[str, str],
    pull_requests: Sequence[PullRequestView],
    failure_reasons: Mapping[str, str],
    runs: Sequence[RunView],
) -> AdvanceProjection:
    states: list[ResolvedTaskState] = []
    fallbacks: list[tuple[str, str]] = []
    for task in tasks:
        pull = _matching_pr(task, pull_requests)
        state, fallback = _state(
            request=request,
            task=task,
            base_target=base_targets.get(task.task_key or "", "missing"),
            head_target=head_targets.get(task.task_key or "", "missing"),
            pull_request=pull,
            failure_reason=failure_reasons.get(task.task_key or "", ""),
            runs=runs,
        )
        states.append(state)
        if fallback:
            fallbacks.append(fallback)
    if any(state.status == "running" for state in states):
        return AdvanceProjection(
            action="active-worker-exists",
            states=tuple(states),
            next_task=None,
            fallback_failures=tuple(fallbacks),
        )
    next_task = next(
        (
            task
            for task, state in zip(tasks, states)
            if state.status == "pending"
        ),
        None,
    )
    return AdvanceProjection(
        action="dispatch" if next_task else "finalize",
        states=tuple(states),
        next_task=next_task,
        fallback_failures=tuple(fallbacks),
    )
