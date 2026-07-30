"""PR evidence composition and push/create cleanup orchestration."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from scripts.lib.candidate_bundle import CandidateBundle, promote_candidate
from scripts.lib.git_workspace import TargetWorkspace
from scripts.lib.gitcode_client import DeliveryConfig, GitCodeClient


TARGET_SOURCE = "https://gitcode.com/openeuler/openeuler-docker-images.git"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID_RE = re.compile(r"^[1-9][0-9]*$")
_REF_CHARS_RE = re.compile(r"^[A-Za-z0-9._+/-]+$")

GitRunner = Callable[
    [Path, Sequence[str], Mapping[str, str]],
    subprocess.CompletedProcess,
]


class PRDeliveryError(RuntimeError):
    """Raised when delivery policy refuses or cleanup cannot restore safety."""


class GitDeliveryError(RuntimeError):
    """Raised when a branch delivery cannot be performed safely."""


class ForkPRPipelineError(RuntimeError):
    """Raised when fork delivery is not configured safely."""


@dataclass(frozen=True)
class PullRequestContent:
    title: str
    body: str


def _validate_branch(branch: str) -> str:
    if not branch.startswith("auto/"):
        raise GitDeliveryError("working branch must be under auto/")
    if not _REF_CHARS_RE.fullmatch(branch):
        raise GitDeliveryError("working branch contains unsafe ref characters")
    segments = branch.split("/")
    if any(
        not segment
        or segment in {".", ".."}
        or segment.startswith(".")
        or segment.endswith((".", ".lock"))
        for segment in segments
    ):
        raise GitDeliveryError("working branch is not a safe Git ref")
    if ".." in branch or "@{" in branch:
        raise GitDeliveryError("working branch is not a safe Git ref")
    return f"refs/heads/{branch}"


def _remote_url(config: DeliveryConfig) -> str:
    return f"https://gitcode.com/{config.push_repo}.git"


def _default_git_runner(
    repo: Path,
    args: Sequence[str],
    env: Mapping[str, str],
) -> subprocess.CompletedProcess:
    process_env = os.environ.copy()
    process_env.update(env)
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
        env=process_env,
    )


@contextmanager
def _credential_environment(
    token: str,
    username: str,
) -> Iterator[dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="oe-gitcode-askpass-") as directory:
        askpass = Path(directory) / "askpass.sh"
        askpass.write_text(
            "\n".join(
                (
                    "#!/bin/sh",
                    'case "$1" in',
                    '  *sername*) printf "%s\\n" "$OE_GITCODE_USERNAME" ;;',
                    '  *assword*) printf "%s\\n" "$OE_GITCODE_TOKEN" ;;',
                    "  *) exit 1 ;;",
                    "esac",
                    "",
                )
            )
        )
        askpass.chmod(0o700)
        yield {
            "GIT_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "0",
            "OE_GITCODE_USERNAME": username,
            "OE_GITCODE_TOKEN": token,
        }


def _run_git(
    runner: GitRunner,
    repo: Path,
    args: Sequence[str],
    env: Mapping[str, str],
    token: str,
) -> subprocess.CompletedProcess:
    result = runner(repo, args, env)
    if result.returncode != 0:
        detail = str(result.stderr or result.stdout or "Git command failed")
        detail = detail.replace(token, "REDACTED").strip()
        raise GitDeliveryError(detail)
    return result


def _observed_remote_sha(
    *,
    runner: GitRunner,
    repo: Path,
    remote: str,
    ref: str,
    env: Mapping[str, str],
    token: str,
) -> str:
    result = _run_git(
        runner,
        repo,
        ["ls-remote", remote, ref],
        env,
        token,
    )
    output = str(result.stdout or "").strip()
    if not output:
        return ""
    lines = output.splitlines()
    if len(lines) != 1:
        raise GitDeliveryError("ls-remote returned multiple branch matches")
    fields = lines[0].split()
    if len(fields) != 2 or fields[1] != ref or not _SHA_RE.fullmatch(fields[0]):
        raise GitDeliveryError("ls-remote returned an invalid branch record")
    return fields[0]


def _delivery_preflight(
    *,
    repo: Path,
    config: DeliveryConfig,
    branch: str,
    username: str,
    token: str,
) -> tuple[Path, str]:
    if not config.allows_branch_push:
        raise GitDeliveryError(
            f"delivery mode {config.delivery_mode} forbids branch writes"
        )
    if not token:
        raise GitDeliveryError("GitCode token is required")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", username):
        raise GitDeliveryError("GitCode username is required and must be safe")
    repo = Path(repo)
    if not repo.is_dir():
        raise GitDeliveryError("Git workspace does not exist")
    return repo, _validate_branch(branch)


def push_working_branch(
    *,
    repo: Path,
    config: DeliveryConfig,
    branch: str,
    username: str,
    token: str,
    runner: GitRunner = _default_git_runner,
) -> None:
    repo, ref = _delivery_preflight(
        repo=repo,
        config=config,
        branch=branch,
        username=username,
        token=token,
    )
    remote = _remote_url(config)
    with _credential_environment(token, username) as env:
        observed_sha = _observed_remote_sha(
            runner=runner,
            repo=repo,
            remote=remote,
            ref=ref,
            env=env,
            token=token,
        )
        _run_git(
            runner,
            repo,
            [
                "push",
                f"--force-with-lease={ref}:{observed_sha}",
                remote,
                f"HEAD:{ref}",
            ],
            env,
            token,
        )


def delete_working_branch(
    *,
    repo: Path,
    config: DeliveryConfig,
    branch: str,
    username: str,
    token: str,
    runner: GitRunner = _default_git_runner,
) -> bool:
    repo, ref = _delivery_preflight(
        repo=repo,
        config=config,
        branch=branch,
        username=username,
        token=token,
    )
    remote = _remote_url(config)
    with _credential_environment(token, username) as env:
        observed_sha = _observed_remote_sha(
            runner=runner,
            repo=repo,
            remote=remote,
            ref=ref,
            env=env,
            token=token,
        )
        if not observed_sha:
            return False
        _run_git(
            runner,
            repo,
            [
                "push",
                f"--force-with-lease={ref}:{observed_sha}",
                remote,
                f":{ref}",
            ],
            env,
            token,
        )
        return True


def _load_json(path: Path, label: str) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise PRDeliveryError(f"{label} evidence is invalid") from error
    if not isinstance(payload, dict):
        raise PRDeliveryError(f"{label} evidence must be an object")
    return payload


def _brief(value: object, *, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "No summary recorded."
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _qa_review_lines(root: Path, prefix: str, label: str) -> tuple[str, ...]:
    def round_number(path: Path) -> int:
        match = re.search(r"-round([0-9]+)\.json$", path.name)
        return int(match.group(1)) if match else 0

    paths = sorted(
        (root / "reports" / "agents").glob(f"{prefix}-round*.json"),
        key=round_number,
    )
    lines = [f"### {label} review", ""]
    if not paths:
        return (*lines, "- Final result: `not recorded`.", "")

    reports = []
    for path in paths:
        report = _load_json(path, f"{prefix} round {round_number(path)}")
        status = str(report.get("status", "unknown"))
        if status not in {"approved", "needs_fix"}:
            raise PRDeliveryError(f"{prefix} evidence has an invalid status")
        reports.append((round_number(path), status, report))

    final_status = reports[-1][1]
    rounds = len(reports)
    suffix = "round" if rounds == 1 else "rounds"
    lines.append(f"- Final result: `{final_status}` after {rounds} {suffix}.")
    coverage = reports[-1][2].get("coverage_score")
    if coverage is not None:
        lines.append(f"- Coverage score: `{coverage}`.")
    for number, status, report in reports:
        lines.append(
            f"- Round {number}: `{status}` — {_brief(report.get('summary'))}"
        )
        issues = report.get("issues")
        if not isinstance(issues, list):
            continue
        for issue in issues[:3]:
            if not isinstance(issue, dict):
                continue
            severity = _brief(issue.get("severity", "unknown"), limit=30)
            description = _brief(issue.get("description"), limit=300)
            file = (
                _brief(issue.get("file"), limit=200)
                if issue.get("file")
                else ""
            )
            location = f"`{file}`: " if file else ""
            lines.append(f"  - `{severity}` — {location}{description}")
        if len(issues) > 3:
            lines.append(
                f"  - {len(issues) - 3} more findings are preserved in "
                "the validation artifact."
            )
    lines.append("")
    return tuple(lines)


def _patch_changes(bundle: CandidateBundle) -> tuple[tuple[str, str], ...]:
    try:
        lines = (bundle.root / "changes.patch").read_text().splitlines()
    except OSError as error:
        raise PRDeliveryError("candidate patch is unavailable") from error

    changes: list[tuple[str, str]] = []
    path = ""
    status = "Modified"

    def record() -> None:
        if path:
            changes.append((status, path))

    for line in lines:
        match = re.fullmatch(r"diff --git a/(.+) b/(.+)", line)
        if match:
            record()
            path = match.group(2)
            status = "Modified"
        elif line.startswith("new file mode "):
            status = "Added"
        elif line.startswith("deleted file mode "):
            status = "Deleted"
        elif line.startswith("rename to "):
            status = "Renamed"
            path = line.removeprefix("rename to ")
    record()
    if not changes:
        raise PRDeliveryError("candidate patch contains no file changes")
    return tuple(changes)


def _recovery_provenance(
    bundle: CandidateBundle,
) -> Mapping[str, object] | None:
    path = bundle.root / "reports" / "recovery-provenance.json"
    if not path.is_file():
        return None
    recovery = _load_json(path, "recovery provenance")
    expected_fields = {
        "schema_version",
        "mode",
        "status",
        "generation_run_id",
        "validation_run_id",
        "packaging_run_id",
        "validated_base_sha",
        "current_base_sha",
        "upstream_changed_paths",
        "candidate_changed_paths",
        "promotable",
    }
    if set(recovery) != expected_fields:
        raise PRDeliveryError("recovery provenance has an invalid schema")
    if (
        recovery.get("schema_version") != 1
        or recovery.get("mode") != "recover_package"
        or recovery.get("status") != "passed"
        or recovery.get("promotable") is not True
    ):
        raise PRDeliveryError("recovery provenance is not promotable")
    for field in (
        "generation_run_id",
        "validation_run_id",
        "packaging_run_id",
    ):
        if not _RUN_ID_RE.fullmatch(str(recovery.get(field, ""))):
            raise PRDeliveryError(f"recovery provenance {field} is invalid")
    if recovery["packaging_run_id"] != bundle.manifest.validated_run_id:
        raise PRDeliveryError(
            "recovery packaging run does not match candidate manifest"
        )
    if recovery["current_base_sha"] != bundle.manifest.base_sha:
        raise PRDeliveryError(
            "recovery current base does not match candidate manifest"
        )
    if not _SHA_RE.fullmatch(str(recovery["validated_base_sha"])):
        raise PRDeliveryError("recovery validated base SHA is invalid")
    upstream = recovery["upstream_changed_paths"]
    candidate = recovery["candidate_changed_paths"]
    if (
        not isinstance(upstream, list)
        or not isinstance(candidate, list)
        or not candidate
        or any(not isinstance(path, str) or not path for path in upstream)
        or any(not isinstance(path, str) or not path for path in candidate)
    ):
        raise PRDeliveryError("recovery changed paths are invalid")
    if set(upstream) & set(candidate):
        raise PRDeliveryError("recovery changed paths overlap")
    return recovery


def compose_pull_request(bundle: CandidateBundle) -> PullRequestContent:
    task = bundle.task
    changes = _patch_changes(bundle)
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
    image_review = _qa_review_lines(bundle.root, "image-qa", "Image")
    testcase_review = _qa_review_lines(
        bundle.root,
        "testcase-qa",
        "Testcase",
    )
    recovery = _recovery_provenance(bundle)
    recovery_lines: tuple[str, ...] = ()
    candidate_origin = (
        f"- Promoted from validated run "
        f"`{bundle.manifest.validated_run_id}`."
    )
    if recovery is not None:
        candidate_origin = (
            f"- Packaged in recovery run: `{recovery['packaging_run_id']}`."
        )
        recovery_lines = (
            f"- Generation evidence run: "
            f"`{recovery.get('generation_run_id')}`.",
            f"- Native validation evidence run: "
            f"`{recovery.get('validation_run_id')}`.",
            f"- Recovery packaging run: "
            f"`{recovery.get('packaging_run_id')}`.",
            "- Non-overlapping target-base advance: verified.",
        )
    title = (
        f"[New Image] Add {task.app} {task.version} "
        f"for openEuler {task.os_version}"
    )
    body = "\n".join(
        (
            "## Summary",
            "",
            f"Add `{task.app}` `{task.version}` built from "
            f"[the pinned upstream source]({task.source_url}) for openEuler "
            f"{task.os_version}.",
            "",
            f"- Validated run: `{bundle.manifest.validated_run_id}`.",
            f"- Candidate SHA256: `{bundle.manifest.content_sha256}`.",
            "",
            "## Changes",
            "",
            "| Status | File |",
            "|---|---|",
            *(f"| {status} | `{path}` |" for status, path in changes),
            "",
            "## Adversarial review",
            "",
            *image_review,
            *testcase_review,
            "## Repository checks",
            "",
            "| Architecture | Platform | Image ID | Duration | Passed checks |",
            "|---|---|---|---:|---|",
            *rows,
            "",
            candidate_origin,
            *recovery_lines,
            f"- Deterministic target contract: {gates.get('status')}",
            f"- Target base SHA: `{bundle.manifest.base_sha}`.",
            f"- Task ID: `{bundle.manifest.task_id}`.",
            "- Result evidence: `results.json`, `version_info.json`, "
            "`x86_64.junit.xml`, `aarch64.junit.xml`.",
            "- Candidate integrity: verified before delivery.",
            "",
            "## Checklist",
            "",
            "- [x] Version and upstream source are pinned",
            "- [x] Changed files are restricted to the task scope",
            "- [x] x86_64 native validation passed",
            "- [x] aarch64 native validation passed",
            "- [x] Adversarial review records are preserved",
            "- [x] Candidate integrity was verified before delivery",
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


def deliver_validated_candidate(
    *,
    candidate_dir: Path,
    expected_run_id: str,
    workspace_dir: Path,
    target_source: str,
    config: DeliveryConfig,
    username: str,
    token: str,
    delivery_run_id: str,
    delivery_run_attempt: str,
    clone: Callable[..., TargetWorkspace] = TargetWorkspace.clone,
    promote: Callable[..., Any] = promote_candidate,
    client_factory: Callable[..., Any] = GitCodeClient,
    deliver: Callable[..., Any] = deliver_promoted_candidate,
) -> Any:
    if config.environment != "test" or config.delivery_mode != "fork_pr":
        raise ForkPRPipelineError(
            "validated candidate delivery requires test fork_pr mode"
        )
    if not token:
        raise ForkPRPipelineError("GitCode token is required")
    if not username:
        raise ForkPRPipelineError("GitCode username is required")
    if (
        not delivery_run_id.isdigit()
        or int(delivery_run_id) < 1
        or not delivery_run_attempt.isdigit()
        or int(delivery_run_attempt) < 1
    ):
        raise ForkPRPipelineError(
            "delivery run ID and attempt must be positive integers"
        )

    bundle = CandidateBundle.verify(
        candidate_dir,
        expected_run_id=expected_run_id,
    )
    delivery_branch = (
        f"{bundle.task.branch}-e2e-{delivery_run_id}-a{delivery_run_attempt}"
    )
    workspace = clone(
        target_source,
        workspace_dir,
        branch=config.target_branch,
    )
    if workspace.base_sha != bundle.manifest.base_sha:
        raise ForkPRPipelineError(
            "target master changed after validation; run validate_only again"
        )
    promotion = promote(
        candidate_dir=candidate_dir,
        expected_run_id=expected_run_id,
        workspace=workspace,
        branch=delivery_branch,
    )
    content = compose_pull_request(bundle)
    return deliver(
        repo=workspace.path,
        config=config,
        promotion=promotion,
        username=username,
        token=token,
        title=content.title,
        body=content.body,
        client=client_factory(token=token),
    )
