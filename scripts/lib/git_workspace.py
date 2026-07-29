"""Safe Git workspace operations shared by validation and delivery."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class GitWorkspaceError(RuntimeError):
    """Raised when a Git workspace operation cannot be completed safely."""


BOT_NAME = "openEuler Docker Autopilot Bot"
BOT_EMAIL = "openeuler-docker-autopilot@users.noreply.github.com"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _run(repo: Path | None, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    command = ["git"]
    if repo is not None:
        command.extend(["-C", str(repo)])
    command.extend(args)
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=text,
            errors="replace" if text else None,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode()
        raise GitWorkspaceError(stderr.strip() or "Git command failed") from exc


def _validate_clone_source(source: str) -> None:
    parsed = urlparse(source)
    if parsed.scheme:
        if parsed.scheme != "https":
            raise GitWorkspaceError("remote clone source must use HTTPS")
        if parsed.username or parsed.password:
            raise GitWorkspaceError("clone URL must not contain credentials")
    elif "@" in source or source.startswith(("git:", "ssh:")):
        raise GitWorkspaceError("clone URL must not contain credentials")


@dataclass(frozen=True)
class TargetWorkspace:
    path: Path
    branch: str
    base_sha: str

    @classmethod
    def clone(
        cls,
        source: str,
        destination: Path,
        *,
        branch: str,
    ) -> "TargetWorkspace":
        _validate_clone_source(source)
        destination = Path(destination)
        if destination.exists() and any(destination.iterdir()):
            raise GitWorkspaceError("clone destination must be absent or empty")
        destination.parent.mkdir(parents=True, exist_ok=True)
        _run(
            None,
            "clone",
            "--branch",
            branch,
            "--single-branch",
            "--no-tags",
            source,
            str(destination),
        )
        base_sha = _run(destination, "rev-parse", "HEAD").stdout.strip()
        return cls(path=destination, branch=branch, base_sha=base_sha)

    @classmethod
    def clone_at_sha(
        cls,
        source: str,
        destination: Path,
        *,
        branch: str,
        expected_sha: str,
    ) -> "TargetWorkspace":
        if not _SHA_RE.fullmatch(expected_sha):
            raise GitWorkspaceError("expected base SHA must be full lowercase hex")
        cloned = cls.clone(source, destination, branch=branch)
        try:
            _run(cloned.path, "cat-file", "-e", f"{expected_sha}^{{commit}}")
        except GitWorkspaceError as error:
            raise GitWorkspaceError(
                "expected base SHA is unavailable from target master"
            ) from error
        _run(cloned.path, "checkout", "--detach", expected_sha)
        return cls(
            path=cloned.path,
            branch=branch,
            base_sha=expected_sha,
        )

    @classmethod
    def open_existing(
        cls,
        path: Path,
        *,
        branch: str,
        base_sha: str,
    ) -> "TargetWorkspace":
        path = Path(path)
        if not _SHA_RE.fullmatch(base_sha):
            raise GitWorkspaceError("base SHA must be full lowercase hex")
        if not (path / ".git").is_dir():
            raise GitWorkspaceError("existing workspace is not a Git checkout")
        current_sha = _run(path, "rev-parse", "HEAD").stdout.strip()
        if current_sha != base_sha:
            raise GitWorkspaceError(
                "existing workspace HEAD does not match the declared base SHA"
            )
        return cls(path=path, branch=branch, base_sha=base_sha)

    def configure_bot_identity(self) -> None:
        _run(self.path, "config", "user.name", BOT_NAME)
        _run(self.path, "config", "user.email", BOT_EMAIL)

    def create_patch(self, destination: Path) -> None:
        _run(self.path, "add", "--intent-to-add", "--", ".")
        result = _run(
            self.path,
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            self.base_sha,
            "--",
            text=False,
        )
        patch = result.stdout
        if not patch:
            raise GitWorkspaceError("no changes available for candidate patch")
        Path(destination).write_bytes(patch)

    def apply_patch(self, patch: Path) -> None:
        patch = Path(patch)
        if not patch.is_file() or patch.stat().st_size == 0:
            raise GitWorkspaceError("candidate patch is missing or empty")
        _run(self.path, "apply", "--check", "--binary", str(patch))
        _run(self.path, "apply", "--binary", str(patch))

    def commit_candidate(self, *, branch: str, message: str) -> str:
        if not branch.startswith("auto/"):
            raise GitWorkspaceError("candidate branch must be under auto/")
        current_sha = _run(self.path, "rev-parse", "HEAD").stdout.strip()
        if current_sha != self.base_sha:
            raise GitWorkspaceError("workspace HEAD no longer matches its base SHA")
        if not _run(self.path, "status", "--porcelain").stdout.strip():
            raise GitWorkspaceError("candidate workspace has no changes to commit")

        self.configure_bot_identity()
        _run(self.path, "checkout", "-b", branch)
        _run(self.path, "add", "--all", "--", ".")
        if not _run(self.path, "diff", "--cached", "--name-only").stdout.strip():
            raise GitWorkspaceError("candidate has no staged changes")
        _run(
            self.path,
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--no-verify",
            "-m",
            message,
        )
        parent_sha = _run(self.path, "rev-parse", "HEAD^").stdout.strip()
        if parent_sha != self.base_sha:
            raise GitWorkspaceError("candidate commit parent is not the validated base")
        if _run(self.path, "status", "--porcelain").stdout.strip():
            raise GitWorkspaceError("candidate workspace is dirty after commit")
        return _run(self.path, "rev-parse", "HEAD").stdout.strip()
