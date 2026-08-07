import subprocess
from pathlib import Path

import pytest


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _upstream(tmp_path):
    repo = tmp_path / "upstream"
    subprocess.run(
        ["git", "init", "-b", "master", str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.com")
    (repo / "README.md").write_text("upstream\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_workspace_clones_master_and_records_exact_base(tmp_path):
    from scripts.lib.git_workspace import TargetWorkspace

    upstream = _upstream(tmp_path)
    expected_sha = _git(upstream, "rev-parse", "HEAD")

    workspace = TargetWorkspace.clone(
        str(upstream),
        tmp_path / "workspace",
        branch="master",
    )

    assert workspace.base_sha == expected_sha
    assert workspace.branch == "master"
    assert (workspace.path / "README.md").read_text() == "upstream\n"


def test_workspace_retries_one_interrupted_clone_and_cleans_partial_checkout(
    tmp_path,
    monkeypatch,
):
    from scripts.lib import git_workspace
    from scripts.lib.git_workspace import TargetWorkspace

    upstream = _upstream(tmp_path)
    destination = tmp_path / "workspace"
    real_run = subprocess.run
    attempts = 0

    def interrupt_once(command, **kwargs):
        nonlocal attempts
        if command[:2] == ["git", "clone"]:
            attempts += 1
            if attempts == 1:
                destination.mkdir()
                (destination / "partial-pack").write_text("incomplete")
                raise subprocess.CalledProcessError(
                    128,
                    command,
                    stderr=(
                        "error: RPC failed; curl 18 transfer closed with "
                        "outstanding read data remaining\n"
                        "fatal: early EOF"
                    ),
                )
        return real_run(command, **kwargs)

    monkeypatch.setattr(git_workspace.subprocess, "run", interrupt_once)

    workspace = TargetWorkspace.clone(
        str(upstream),
        destination,
        branch="master",
    )

    assert attempts == 2
    assert workspace.path == destination
    assert not (destination / "partial-pack").exists()
    assert (destination / "README.md").read_text() == "upstream\n"


def test_workspace_does_not_retry_a_non_transient_clone_error(
    tmp_path,
    monkeypatch,
):
    from scripts.lib import git_workspace
    from scripts.lib.git_workspace import GitWorkspaceError, TargetWorkspace

    attempts = 0

    def repository_not_found(command, **kwargs):
        nonlocal attempts
        attempts += 1
        raise subprocess.CalledProcessError(
            128,
            command,
            stderr="fatal: repository not found",
        )

    monkeypatch.setattr(git_workspace.subprocess, "run", repository_not_found)

    with pytest.raises(GitWorkspaceError, match="repository not found"):
        TargetWorkspace.clone(
            str(tmp_path / "missing"),
            tmp_path / "workspace",
            branch="master",
        )

    assert attempts == 1


def test_workspace_marks_exhausted_interrupted_clone_as_transient(
    tmp_path,
    monkeypatch,
):
    from scripts.lib import git_workspace
    from scripts.lib.git_workspace import (
        TransientGitWorkspaceError,
        TargetWorkspace,
    )

    attempts = 0

    def always_disconnect(command, **kwargs):
        nonlocal attempts
        attempts += 1
        raise subprocess.CalledProcessError(
            128,
            command,
            stderr="fetch-pack: unexpected disconnect\nfatal: early EOF",
        )

    monkeypatch.setattr(git_workspace.subprocess, "run", always_disconnect)

    with pytest.raises(TransientGitWorkspaceError, match="2 attempts"):
        TargetWorkspace.clone(
            str(tmp_path / "upstream"),
            tmp_path / "workspace",
            branch="master",
        )

    assert attempts == 2


def test_workspace_rejects_clone_url_with_credentials(tmp_path):
    from scripts.lib.git_workspace import GitWorkspaceError, TargetWorkspace

    with pytest.raises(GitWorkspaceError, match="credentials"):
        TargetWorkspace.clone(
            "https://oauth2:secret@gitcode.com/openeuler/repo.git",
            tmp_path / "workspace",
            branch="master",
        )


def test_workspace_generates_and_replays_text_and_binary_patch(tmp_path):
    from scripts.lib.git_workspace import TargetWorkspace

    upstream = _upstream(tmp_path)
    generated = TargetWorkspace.clone(
        str(upstream),
        tmp_path / "generated",
        branch="master",
    )
    (generated.path / "Database" / "kvrocks").mkdir(parents=True)
    (generated.path / "Database" / "kvrocks" / "README.md").write_text(
        "Kvrocks\n"
    )
    (generated.path / "Database" / "kvrocks" / "logo.png").write_bytes(
        b"\x89PNG\r\n\x1a\nbinary-logo"
    )
    patch = tmp_path / "changes.patch"

    generated.create_patch(patch)
    replay = TargetWorkspace.clone(
        str(upstream),
        tmp_path / "replay",
        branch="master",
    )
    replay.apply_patch(patch)

    assert (replay.path / "Database" / "kvrocks" / "README.md").read_text() == (
        "Kvrocks\n"
    )
    assert (replay.path / "Database" / "kvrocks" / "logo.png").read_bytes() == (
        b"\x89PNG\r\n\x1a\nbinary-logo"
    )


def test_workspace_rejects_empty_patch_creation(tmp_path):
    from scripts.lib.git_workspace import GitWorkspaceError, TargetWorkspace

    upstream = _upstream(tmp_path)
    workspace = TargetWorkspace.clone(
        str(upstream),
        tmp_path / "workspace",
        branch="master",
    )

    with pytest.raises(GitWorkspaceError, match="no changes"):
        workspace.create_patch(tmp_path / "changes.patch")


def test_workspace_configures_confirmed_bot_identity(tmp_path):
    from scripts.lib.git_workspace import TargetWorkspace

    upstream = _upstream(tmp_path)
    workspace = TargetWorkspace.clone(
        str(upstream),
        tmp_path / "workspace",
        branch="master",
    )
    workspace.configure_bot_identity()

    assert _git(workspace.path, "config", "user.name") == (
        "openEuler Docker Autopilot Bot"
    )
    assert _git(workspace.path, "config", "user.email") == (
        "jcccx.cmh@gmail.com"
    )


def test_workspace_refuses_nonempty_destination(tmp_path):
    from scripts.lib.git_workspace import GitWorkspaceError, TargetWorkspace

    upstream = _upstream(tmp_path)
    destination = tmp_path / "workspace"
    destination.mkdir()
    (destination / "owned-by-user").write_text("keep")

    with pytest.raises(GitWorkspaceError, match="destination"):
        TargetWorkspace.clone(str(upstream), destination, branch="master")
    assert (destination / "owned-by-user").read_text() == "keep"


def test_workspace_can_clone_an_exact_earlier_master_sha(tmp_path):
    from scripts.lib.git_workspace import TargetWorkspace

    upstream = _upstream(tmp_path)
    expected_sha = _git(upstream, "rev-parse", "HEAD")
    (upstream / "README.md").write_text("newer upstream\n")
    _git(upstream, "add", "README.md")
    _git(upstream, "commit", "-m", "newer target commit")

    workspace = TargetWorkspace.clone_at_sha(
        str(upstream),
        tmp_path / "workspace",
        branch="master",
        expected_sha=expected_sha,
    )

    assert workspace.base_sha == expected_sha
    assert _git(workspace.path, "rev-parse", "HEAD") == expected_sha
    assert (workspace.path / "README.md").read_text() == "upstream\n"


def test_workspace_rejects_unavailable_exact_sha(tmp_path):
    from scripts.lib.git_workspace import GitWorkspaceError, TargetWorkspace

    upstream = _upstream(tmp_path)

    with pytest.raises(GitWorkspaceError, match="expected base"):
        TargetWorkspace.clone_at_sha(
            str(upstream),
            tmp_path / "workspace",
            branch="master",
            expected_sha="a" * 40,
        )


def test_workspace_opens_existing_checkout_only_at_declared_base(tmp_path):
    from scripts.lib.git_workspace import GitWorkspaceError, TargetWorkspace

    upstream = _upstream(tmp_path)
    workspace = TargetWorkspace.clone(
        str(upstream),
        tmp_path / "workspace",
        branch="master",
    )

    opened = TargetWorkspace.open_existing(
        workspace.path,
        branch="master",
        base_sha=workspace.base_sha,
    )

    assert opened == workspace

    with pytest.raises(GitWorkspaceError, match="HEAD"):
        TargetWorkspace.open_existing(
            workspace.path,
            branch="master",
            base_sha="a" * 40,
        )


def _candidate_patch(upstream, tmp_path, relative_path="Database/kvrocks/README.md"):
    from scripts.lib.git_workspace import TargetWorkspace

    generated = TargetWorkspace.clone(
        str(upstream),
        tmp_path / f"generated-{len(list(tmp_path.iterdir()))}",
        branch="master",
    )
    candidate = generated.path / relative_path
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("candidate\n")
    patch = tmp_path / f"candidate-{len(list(tmp_path.iterdir()))}.patch"
    generated.create_patch(patch)
    return patch


