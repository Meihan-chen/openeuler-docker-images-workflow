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


def _last_qa_status(root: Path, prefix: str) -> str:
    paths = sorted((root / "reports" / "agents").glob(f"{prefix}-round*.json"))
    if not paths:
        return "not recorded"
    report = _load_json(paths[-1], prefix)
    status = str(report.get("status", "unknown"))
    if status not in {"approved", "needs_fix"}:
        raise PRDeliveryError(f"{prefix} evidence has an invalid status")
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


def deliver_validated_candidate(
    *,
    candidate_dir: Path,
    expected_run_id: str,
    workspace_dir: Path,
    target_source: str,
    config: DeliveryConfig,
    username: str,
    token: str,
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

    bundle = CandidateBundle.verify(
        candidate_dir,
        expected_run_id=expected_run_id,
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
