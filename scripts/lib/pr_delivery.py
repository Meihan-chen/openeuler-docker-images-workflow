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

from scripts.lib.candidate_bundle import (
    CandidateBundle,
    junit_pass_rate,
    promote_candidate,
)
from scripts.lib.git_workspace import TargetWorkspace
from scripts.lib.gitcode_client import DeliveryConfig, GitCodeClient
from scripts.lib.target_contract import native_checks_pass


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


def calculate_confidence(
    build_success: Mapping[str, bool],
    test_pass_rate: Mapping[str, float],
    hadolint_violations: int,
    meta_consistent: bool,
) -> dict[str, object]:
    """Score only evidence already required by validated PR delivery."""
    build_score = 0.35
    if not build_success.get("amd64", False):
        build_score *= 0.5
    if not build_success.get("arm64", False):
        build_score *= 0.5
    test_average = (
        test_pass_rate.get("amd64", 0) + test_pass_rate.get("arm64", 0)
    ) / 2
    test_score = 0.30 * test_average
    lint_score = 0.20 * max(0, 1 - hadolint_violations * 0.1)
    meta_score = 0.15 if meta_consistent else 0
    total = build_score + test_score + lint_score + meta_score
    level = "auto-merge" if total >= 0.85 else "review" if total >= 0.75 else "loop"
    return {
        "score": round(total, 3),
        "level": level,
        "details": {
            "build": round(build_score, 3),
            "test": round(test_score, 3),
            "lint": round(lint_score, 3),
            "meta": round(meta_score, 3),
        },
    }


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


def _hadolint_lines(root: Path) -> tuple[str, ...]:
    path = root / "reports" / "hadolint.txt"
    if not path.is_file():
        raise PRDeliveryError("Hadolint evidence is required")
    output = path.read_text(errors="replace")
    finding_lines = [
        line.strip()
        for line in output.splitlines()
        if re.search(r"\b(?:DL|SC)[0-9]{4}\b", line)
    ]
    if not finding_lines:
        return ("- Hadolint: `0` advisory findings.",)
    lines = [f"- Hadolint: `{len(finding_lines)}` advisory findings."]
    lines.extend(f"  - `{_brief(line)}`" for line in finding_lines[:3])
    if len(finding_lines) > 3:
        lines.append(
            f"  - {len(finding_lines) - 3} more findings are preserved in "
            "the candidate artifact."
        )
    return tuple(lines)


def _hadolint_violation_count(root: Path) -> int:
    path = root / "reports" / "hadolint.txt"
    if not path.is_file():
        raise PRDeliveryError("Hadolint evidence is required")
    return sum(
        1
        for line in path.read_text(errors="replace").splitlines()
        if re.search(r"\b(?:DL|SC)[0-9]{4}\b", line)
    )


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
        raise PRDeliveryError(f"{prefix} QA evidence is required")

    reports = []
    for path in paths:
        report = _load_json(path, f"{prefix} round {round_number(path)}")
        status = str(report.get("status", "unknown"))
        if status not in {"approved", "needs_fix"}:
            raise PRDeliveryError(f"{prefix} evidence has an invalid status")
        reports.append((round_number(path), status, report))

    sequence = [(number, status) for number, status, _ in reports]
    valid_sequence = sequence == [(1, "approved")] or (
        len(sequence) == 2
        and sequence[0] == (1, "needs_fix")
        and sequence[1][0] == 2
    )
    if not valid_sequence:
        raise PRDeliveryError(f"{prefix} QA round sequence is invalid")

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
        harness = report.get("harness")
        if not isinstance(harness, dict):
            harness = {}
        snapshot = harness.get("snapshot")
        if isinstance(snapshot, dict):
            snapshot_status = _brief(snapshot.get("status"), limit=30)
            if snapshot_status == "full":
                binaries = snapshot.get("hashed_binary_files")
                if isinstance(binaries, list) and binaries:
                    lines.append(
                        "  - Review input: `full text`; "
                        f"`{len(binaries)}` binary file(s) were hash-only."
                    )
                else:
                    lines.append("  - Review input: `full`.")
            elif snapshot_status:
                lines.append(
                    f"  - Review input: `{snapshot_status}` (partial)."
                )
                compacted = snapshot.get("compacted_files")
                if isinstance(compacted, list) and compacted:
                    visible = ", ".join(
                        f"`{_brief(path, limit=120)}`"
                        for path in compacted[:3]
                    )
                    lines.append(f"  - Compacted files: {visible}.")
        warnings = harness.get("protocol_warnings")
        if isinstance(warnings, list):
            for warning in warnings:
                if not isinstance(warning, dict):
                    continue
                message = _brief(warning.get("message"), limit=300)
                if message:
                    lines.append(f"  - Protocol warning — {message}")
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


def _fixer_process_lines(root: Path) -> tuple[str, ...]:
    pattern = re.compile(
        r"^fixer-native-(?P<architecture>[A-Za-z0-9_]+)"
        r"-round(?P<round>[0-9]+)"
        r"(?:-attempt(?P<attempt>[0-9]+))?\.json$"
    )

    def report_order(path: Path) -> tuple[int, int, str]:
        match = pattern.fullmatch(path.name)
        if match is None:
            raise PRDeliveryError("native Fixer evidence filename is invalid")
        return (
            int(match.group("round")),
            int(match.group("attempt") or 1),
            match.group("architecture"),
        )

    paths = sorted(
        (root / "reports" / "agents").rglob("fixer-native-*.json"),
        key=report_order,
    )
    lines = [
        "## Fixer process",
        "",
        f"- Native Fixer invocations: `{len(paths)}`.",
    ]
    if not paths:
        return (
            *lines,
            "- No native repair was required.",
            "- Final outcome: the candidate passed sealed native validation "
            "on `x86_64` and `aarch64`.",
            "",
        )

    for path in paths:
        match = pattern.fullmatch(path.name)
        if match is None:
            raise PRDeliveryError("native Fixer evidence filename is invalid")
        report = _load_json(path, "native Fixer")
        if report.get("success") is not True:
            raise PRDeliveryError("native Fixer evidence did not succeed")
        review = report.get("_input_review")
        if not isinstance(review, dict):
            raise PRDeliveryError("native Fixer input review is invalid")
        kind = _brief(review.get("kind", "unknown"), limit=80)
        status = _brief(report.get("status", "fixed"), limit=30)
        round_number = int(match.group("round"))
        attempt = int(match.group("attempt") or 1)
        lines.append(
            f"- Round {round_number}, attempt {attempt}: `{status}` — "
            f"{_brief(report.get('summary'))}"
        )

        architectures = review.get("architectures")
        failed_architectures: list[str] = []
        if isinstance(architectures, dict):
            failed_architectures = [
                str(name)
                for name, evidence in architectures.items()
                if not isinstance(evidence, dict)
                or evidence.get("status") != "passed"
            ]
            architecture_order = {"x86_64": 0, "aarch64": 1}
            failed_architectures.sort(
                key=lambda name: (architecture_order.get(name, 2), name)
            )
        elif review.get("architecture"):
            failed_architectures = [str(review["architecture"])]
        if failed_architectures:
            failed = ", ".join(
                f"`{_brief(name, limit=30)}`"
                for name in failed_architectures
            )
            lines.append(
                f"  - Trigger: `{kind}`; failed architectures: {failed}."
            )
        else:
            lines.append(f"  - Trigger: `{kind}`.")

        changes = report.get("changes")
        if not isinstance(changes, list):
            raise PRDeliveryError("native Fixer changes are invalid")
        for change in changes[:3]:
            if isinstance(change, dict):
                description = _brief(change.get("change"))
                file = change.get("file")
                location = (
                    f"`{_brief(file, limit=200)}` — " if file else ""
                )
                lines.append(f"  - Change: {location}{description}")
            else:
                lines.append(f"  - Change: {_brief(change)}")
        if len(changes) > 3:
            lines.append(
                f"  - {len(changes) - 3} more changes are preserved in "
                "the validation artifact."
            )

    lines.extend(
        (
            "- Final outcome: the repaired candidate passed sealed native "
            "validation on `x86_64` and `aarch64`.",
            "",
        )
    )
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
        if not native_checks_pass(checks):
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
    if gates.get("delivery_allowed") is not True:
        raise PRDeliveryError("deterministic delivery contract did not pass")
    testcase_review = _qa_review_lines(
        bundle.root,
        "testcase-qa",
        "Testcase",
    )
    fixer_process = _fixer_process_lines(bundle.root)
    hadolint_lines = _hadolint_lines(bundle.root)
    hadolint_violations = _hadolint_violation_count(bundle.root)
    confidence = calculate_confidence(
        {"amd64": True, "arm64": True},
        {
            "amd64": junit_pass_rate(
                bundle.root / "reports" / "x86_64.junit.xml",
                "x86_64",
            ),
            "arm64": junit_pass_rate(
                bundle.root / "reports" / "aarch64.junit.xml",
                "aarch64",
            ),
        },
        hadolint_violations,
        True,
    )
    candidate_origin = (
        f"- Promoted from validated run "
        f"`{bundle.manifest.validated_run_id}`."
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
            *testcase_review,
            *fixer_process,
            "## Repository checks",
            "",
            "| Architecture | Platform | Image ID | Duration | Passed checks |",
            "|---|---|---|---:|---|",
            *rows,
            "",
            candidate_origin,
            f"- Deterministic target contract: {gates.get('status')}",
            *hadolint_lines,
            "",
            "### Confidence Score",
            "",
            f"- Score: `{confidence['score']}` (`{confidence['level']}`).",
            "- Inputs: both native builds passed; both required runtime test "
            f"sets passed; Hadolint advisories: "
            f"`{hadolint_violations}`; final metadata "
            "contract passed.",
            f"- Target base SHA: `{bundle.manifest.base_sha}`.",
            f"- Task ID: `{bundle.manifest.task_id}`.",
            "- Target evidence: `version_info.json`, `x86_64.junit.xml`, "
            "`aarch64.junit.xml`.",
            "- Production candidate evidence: `reports/results.json`.",
            "- Candidate integrity: verified before delivery.",
            "",
            "## Checklist",
            "",
            "- [x] Version and upstream source are pinned",
            "- [x] Changed files are restricted to the task scope",
            "- [x] x86_64 native validation passed",
            "- [x] aarch64 native validation passed",
            "- [x] Adversarial review records are preserved",
            "- [x] Native Fixer process is recorded",
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
    issue_number: int | None = None,
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
        create_args = {
            "config": config,
            "title": title,
            "body": body,
            "branch": promotion.branch,
        }
        if issue_number is not None:
            create_args["issue_number"] = issue_number
        return client.create_pull_request(
            **create_args,
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
    source_issue_number: int | None = None,
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
    if source_issue_number is not None and source_issue_number <= 0:
        raise ForkPRPipelineError("source Issue number must be positive")

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
    promotion = promote(
        candidate_dir=candidate_dir,
        expected_run_id=expected_run_id,
        workspace=workspace,
        branch=delivery_branch,
    )
    content = compose_pull_request(bundle)
    client = client_factory(token=token)
    return deliver(
        repo=workspace.path,
        config=config,
        promotion=promotion,
        username=username,
        token=token,
        title=content.title,
        body=content.body,
        client=client,
        issue_number=source_issue_number,
    )
