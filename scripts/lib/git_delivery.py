"""Credential-safe, lease-protected GitCode branch delivery."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

from scripts.lib.delivery_config import DeliveryConfig


class GitDeliveryError(RuntimeError):
    """Raised when a branch delivery cannot be performed safely."""


GitRunner = Callable[
    [Path, Sequence[str], Mapping[str, str]],
    subprocess.CompletedProcess,
]

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REF_CHARS_RE = re.compile(r"^[A-Za-z0-9._+/-]+$")


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


def _default_runner(
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


def _run(
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
    result = _run(
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


def _preflight(
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
    runner: GitRunner = _default_runner,
) -> None:
    repo, ref = _preflight(
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
        _run(
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
    runner: GitRunner = _default_runner,
) -> bool:
    repo, ref = _preflight(
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
        _run(
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
