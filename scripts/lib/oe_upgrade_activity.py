"""Stable markers and receipts for one openEuler upgrade activity."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from scripts.lib.oe_upgrade_contract import UpgradeRequest
from scripts.lib.task_spec import TaskSpec


class ActivityError(RuntimeError):
    """Raised when persisted activity facts are ambiguous or untrusted."""


_REQUEST_RE = re.compile(
    r"<!-- oe-upgrade-request:v1:(?P<key>[0-9a-f]{16}) -->"
    r".*?```json\s*(?P<payload>\{.*?\})\s*```",
    re.DOTALL,
)
_FAILURE_RE = re.compile(
    r"<!-- oe-upgrade-failure:(?P<request>[0-9a-f]{16}):"
    r"(?P<task>[0-9a-f]{16}) -->"
)
_REASON_RE = re.compile(
    r"^(?:-\s*)?Reason:\s*`(?P<reason>[a-z-]+)`\s*$", re.MULTILINE
)
_REASON_VALUES = {
    "generation",
    "contract",
    "build",
    "os-identity",
    "runtime-test",
    "repair-exhausted",
    "evidence-insufficient",
    "dependency",
    "infrastructure",
    "delivery",
    "result-missing",
}
_TERMINAL_STATES = {
    "skipped-existing",
    "pr-created",
    "merged",
    "satisfied-after-base",
    "failed",
}


def _author(comment: Mapping[str, object]) -> str:
    user = comment.get("user")
    if isinstance(user, Mapping):
        return str(user.get("login") or user.get("username") or "")
    return str(comment.get("author") or "")


def request_marker(request_key: str) -> str:
    return f"<!-- oe-upgrade-request:v1:{request_key} -->"


def rejection_marker(issue_number: int, title: str, issue_body: str) -> str:
    """Identify one rejected immutable Issue payload without trusting its fields."""
    digest = hashlib.sha256(
        f"{issue_number}\0{title}\0{issue_body}".encode()
    ).hexdigest()[:16]
    return f"<!-- oe-upgrade-rejection:v1:{issue_number}:{digest} -->"


def task_marker(task_key: str) -> str:
    return f"<!-- oe-upgrade-task:{task_key} -->"


def failure_marker(request_key: str, task_key: str) -> str:
    return f"<!-- oe-upgrade-failure:{request_key}:{task_key} -->"


def planning_path_key(mdu_path: str, oe_version: str) -> str:
    material = f"oe-upgrade\0{mdu_path}\0{oe_version}"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def planning_failure_marker(
    request_key: str, mdu_path: str, oe_version: str
) -> str:
    return (
        "<!-- oe-upgrade-planning-failure:"
        f"{request_key}:{planning_path_key(mdu_path, oe_version)} -->"
    )


def summary_marker(request_key: str, digest: str) -> str:
    return f"<!-- oe-upgrade-summary:{request_key}:{digest} -->"


def render_request_comment(request: UpgradeRequest) -> str:
    return "\n".join(
        (
            "openEuler 大版本升级活动已建立。请求内容在活动期间保持不变。",
            "",
            request_marker(request.request_key),
            "```json",
            json.dumps(request.to_dict(), ensure_ascii=False, sort_keys=True),
            "```",
        )
    )


def render_rejection_comment(
    *, issue_number: int, title: str, issue_body: str, reason: str
) -> str:
    marker = rejection_marker(issue_number, title, issue_body)
    return "\n".join(
        (
            "openEuler 大版本升级请求格式校验失败，自动化未建立活动。",
            "",
            f"- 原因: {_brief(reason, limit=1200)}",
            "- 请修正 Issue 标题和必填字段后，将状态重新设为「新建」。",
            "",
            marker,
        )
    )


def _brief(value: object, *, limit: int = 1200) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_failure_comment(
    *,
    request: UpgradeRequest,
    task: TaskSpec,
    reason: str,
    run_url: str,
    summary: str,
    artifact_name: str,
) -> str:
    if reason not in _REASON_VALUES:
        raise ValueError("unsupported oe-upgrade failure reason")
    if not run_url.startswith("https://"):
        raise ValueError("failure comment run URL must use HTTPS")
    assert task.task_key and task.mdu_path and task.derive_from
    source_oe = task.derive_from.split("/", 1)[1]
    return "\n".join(
        (
            "openEuler 升级任务自动处理失败；本活动不会自动重试该 MDU。",
            "",
            f"- MDU: `{task.mdu_path}`",
            f"- 应用版本: `{task.version}`",
            f"- openEuler: `{source_oe}` → `{task.os_version}`",
            f"- Reason: `{reason}`",
            f"- 摘要: {_brief(summary)}",
            f"- Workflow: {run_url}",
            f"- Evidence artifact: `{_brief(artifact_name, limit=200)}`",
            "",
            failure_marker(request.request_key, task.task_key),
        )
    )


def render_planning_failure_comment(
    *, request: UpgradeRequest, failure: Mapping[str, object]
) -> str:
    mdu_path = str(failure.get("mdu_path", "")).strip()
    reason = _brief(failure.get("reason"), limit=800)
    if not mdu_path:
        raise ValueError("planning failure requires mdu_path")
    return "\n".join(
        (
            "该 MDU 无法形成安全的 openEuler 升级任务。",
            "",
            f"- MDU: `{mdu_path}`",
            f"- Reason: `planning`",
            f"- 摘要: {reason}",
            "",
            planning_failure_marker(
                request.request_key, mdu_path, request.oe_version
            ),
        )
    )


def parse_failure_reasons(
    comments: Sequence[Mapping[str, object]],
    *,
    request_key: str,
    trusted_author: str,
) -> dict[str, str]:
    reasons: dict[str, str] = {}
    for comment in comments:
        if _author(comment) != trusted_author:
            continue
        body = str(comment.get("body", ""))
        marker = _FAILURE_RE.search(body)
        if not marker or marker.group("request") != request_key:
            continue
        reason_match = _REASON_RE.search(body)
        reason = reason_match.group("reason") if reason_match else ""
        if reason not in _REASON_VALUES:
            raise ActivityError("trusted failure comment has an invalid reason")
        task_key = marker.group("task")
        previous = reasons.get(task_key)
        if previous and previous != reason:
            raise ActivityError("trusted failure comments conflict for one task")
        reasons[task_key] = reason
    return reasons


def render_summary_comment(
    *,
    request: UpgradeRequest,
    states: Sequence["ResolvedTaskState"],
    planning_failures: Sequence[Mapping[str, object]],
    run_url: str,
    artifact_name: str,
) -> str:
    digest = state_digest(states, planning_failures)
    counts: dict[str, int] = {
        name: 0
        for name in (
            "pr-created",
            "merged",
            "satisfied-after-base",
            "skipped-existing",
            "failed",
        )
    }
    for state in states:
        if state.status in counts:
            counts[state.status] += 1
    counts["failed"] += len(planning_failures)
    pr_items = [
        (
            f"- `{state.mdu_path}`: "
            f"[PR #{state.pr_number}]({state.pr_url})"
        )
        for state in states
        if state.pr_number is not None and state.pr_url
    ][:20]
    failure_items = [
        f"- `{state.mdu_path}`: `{state.reason}`"
        for state in states
        if state.status == "failed"
    ][:20]
    failure_items.extend(
        f"- `{failure.get('mdu_path', '')}`: `planning`"
        for failure in planning_failures[: max(0, 20 - len(failure_items))]
    )
    return "\n".join(
        (
            "openEuler 大版本升级自动化已完成；Issue 保持打开，等待 PR 审核。",
            "",
            f"- Target openEuler: `{request.oe_version}`",
            f"- Scope: `{', '.join(request.scope)}`",
            f"- Base SHA: `{request.base_sha}`",
            *(f"- {name}: `{counts[name]}`" for name in counts),
            "",
            "### PR 摘要",
            "",
            *(pr_items or ("- 无",)),
            "",
            "### 失败摘要",
            "",
            *(failure_items or ("- 无",)),
            "",
            f"- Final workflow: {run_url}",
            f"- Full result artifact: `{artifact_name}`",
            "",
            summary_marker(request.request_key, digest),
        )
    )


def parse_request_comment(
    comments: Sequence[Mapping[str, object]], *, trusted_author: str
) -> UpgradeRequest:
    matches: list[UpgradeRequest] = []
    for comment in comments:
        if _author(comment) != trusted_author:
            continue
        match = _REQUEST_RE.search(str(comment.get("body", "")))
        if not match:
            continue
        try:
            request = UpgradeRequest.from_json(match.group("payload"))
        except (ValueError, json.JSONDecodeError) as error:
            raise ActivityError("upgrade request comment is invalid") from error
        if match.group("key") != request.request_key:
            raise ActivityError("upgrade request marker does not match payload")
        matches.append(request)
    if not matches:
        raise ActivityError("no trusted upgrade request comment exists")
    if len(matches) != 1:
        raise ActivityError("multiple upgrade requests exist on one Issue")
    return matches[0]


def trusted_comment_has_marker(
    comments: Sequence[Mapping[str, object]], *, marker: str, trusted_author: str
) -> bool:
    return any(
        _author(comment) == trusted_author
        and marker in str(comment.get("body", ""))
        for comment in comments
    )


def ensure_issue_comment(
    *,
    client: Any,
    target_repo: str,
    issue_number: int,
    body: str,
    marker: str,
    trusted_author: str,
) -> bool:
    """Create one marker comment, re-reading after an uncertain POST."""
    comments = client.list_issue_comments(
        target_repo=target_repo,
        number=issue_number,
    )
    if trusted_comment_has_marker(
        comments, marker=marker, trusted_author=trusted_author
    ):
        return False
    if marker not in body:
        raise ActivityError("Issue comment body does not contain its marker")
    try:
        client.create_issue_comment(
            target_repo=target_repo,
            number=issue_number,
            body=body,
        )
    except Exception:
        comments = client.list_issue_comments(
            target_repo=target_repo,
            number=issue_number,
        )
        if trusted_comment_has_marker(
            comments, marker=marker, trusted_author=trusted_author
        ):
            return True
        raise
    return True


def reject_issue_request(
    *,
    client: Any,
    target_repo: str,
    issue: Mapping[str, object],
    reason: str,
    trusted_author: str,
) -> None:
    """Record one invalid payload and keep the Issue in an explicit rejected state."""
    number = int(issue.get("number", issue.get("iid", 0)))
    title = str(issue.get("title", ""))
    issue_body = str(issue.get("body", "") or "")
    marker = rejection_marker(number, title, issue_body)
    ensure_issue_comment(
        client=client,
        target_repo=target_repo,
        issue_number=number,
        body=render_rejection_comment(
            issue_number=number,
            title=title,
            issue_body=issue_body,
            reason=reason,
        ),
        marker=marker,
        trusted_author=trusted_author,
    )
    # Repeat this idempotent write even when the marker already exists.  It
    # repairs the state if a previous comment POST succeeded but status update
    # failed or its response was lost.
    client.update_issue(
        target_repo=target_repo,
        number=number,
        title=title,
        body=issue_body,
        state="open",
        issue_status="已拒绝",
    )


def establish_request(
    *,
    client: Any,
    target_repo: str,
    issue: Mapping[str, object],
    request: UpgradeRequest,
    mode: str,
    trusted_author: str,
) -> bool:
    """Persist a deliver request once; plan mode remains a read-only preview."""
    if mode not in {"plan", "deliver"}:
        raise ValueError("mode must be plan or deliver")
    if mode == "plan":
        return False
    number = int(issue.get("number", issue.get("iid", 0)))
    if number != request.tracking_issue_number:
        raise ActivityError("tracking Issue does not match UpgradeRequest")
    created = ensure_issue_comment(
        client=client,
        target_repo=target_repo,
        issue_number=number,
        body=render_request_comment(request),
        marker=request_marker(request.request_key),
        trusted_author=trusted_author,
    )
    if created:
        client.update_issue(
            target_repo=target_repo,
            number=number,
            title=str(issue.get("title", "")),
            body=str(issue.get("body", "") or ""),
            state="open",
            issue_status="已接纳",
        )
    return created


@dataclass(frozen=True)
class WorkerResult:
    schema_version: int
    request_key: str
    task_key: str
    mdu_path: str
    outcome: str
    reason: str
    run_id: str
    run_url: str
    pr_number: int | None
    pr_url: str
    architectures: tuple[str, ...]
    candidate_digest: str

    @classmethod
    def create(
        cls,
        *,
        request_key: str,
        task: TaskSpec,
        outcome: str,
        reason: str,
        run_id: str,
        run_url: str,
        pr_number: int | None = None,
        pr_url: str = "",
        candidate_digest: str = "",
    ) -> "WorkerResult":
        if outcome not in {"pr-created", "failed"}:
            raise ValueError("outcome must be pr-created or failed")
        if outcome == "failed" and reason not in _REASON_VALUES:
            raise ValueError("failed WorkerResult requires a supported reason")
        if outcome == "pr-created" and reason:
            raise ValueError("successful WorkerResult reason must be empty")
        if not run_id.isdigit() or int(run_id) <= 0:
            raise ValueError("run_id must be positive")
        if not run_url.startswith("https://"):
            raise ValueError("run_url must use HTTPS")
        if outcome == "pr-created" and (
            pr_number is None
            or pr_number <= 0
            or not pr_url.startswith("https://")
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", candidate_digest)
        ):
            raise ValueError("pr-created WorkerResult requires PR and candidate evidence")
        return cls(
            schema_version=1,
            request_key=request_key,
            task_key=task.task_key or "",
            mdu_path=task.mdu_path or "",
            outcome=outcome,
            reason=reason,
            run_id=run_id,
            run_url=run_url,
            pr_number=pr_number,
            pr_url=pr_url,
            architectures=task.architectures,
            candidate_digest=candidate_digest,
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "WorkerResult":
        try:
            value = cls(
                schema_version=int(raw["schema_version"]),
                request_key=str(raw["request_key"]),
                task_key=str(raw["task_key"]),
                mdu_path=str(raw["mdu_path"]),
                outcome=str(raw["outcome"]),
                reason=str(raw["reason"]),
                run_id=str(raw["run_id"]),
                run_url=str(raw["run_url"]),
                pr_number=(
                    int(raw["pr_number"])
                    if raw.get("pr_number") is not None
                    else None
                ),
                pr_url=str(raw.get("pr_url", "")),
                architectures=tuple(raw["architectures"]),
                candidate_digest=str(raw.get("candidate_digest", "")),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("WorkerResult fields are invalid") from error
        if value.schema_version != 1:
            raise ValueError("WorkerResult schema is unsupported")
        return value

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["architectures"] = list(self.architectures)
        return value


@dataclass(frozen=True)
class ResolvedTaskState:
    schema_version: int
    request_key: str
    task_key: str
    mdu_path: str
    status: str
    reason: str
    evidence_source: str
    run_id: str | None
    pr_number: int | None
    pr_url: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("ResolvedTaskState schema is unsupported")
        if self.status not in _TERMINAL_STATES | {"pending", "running"}:
            raise ValueError("ResolvedTaskState status is unsupported")
        if self.status == "failed" and not self.reason:
            raise ValueError("failed state requires a reason")
        if self.pr_number is not None and not self.pr_url.startswith("https://"):
            raise ValueError("PR state requires an HTTPS URL")
        if self.pr_number is None and self.pr_url:
            raise ValueError("PR URL requires a PR number")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def state_digest(
    states: Sequence[ResolvedTaskState],
    planning_failures: Sequence[Mapping[str, object]],
) -> str:
    value = {
        "states": [
            {
                "task_key": state.task_key,
                "status": state.status,
                "reason": state.reason,
                "pr_number": state.pr_number,
            }
            for state in sorted(states, key=lambda item: item.task_key)
        ],
        "planning_failures": sorted(
            (
                {
                    "mdu_path": str(failure.get("mdu_path", "")),
                    "reason": str(failure.get("reason", "")),
                }
                for failure in planning_failures
            ),
            key=lambda item: (item["mdu_path"], item["reason"]),
        ),
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]
