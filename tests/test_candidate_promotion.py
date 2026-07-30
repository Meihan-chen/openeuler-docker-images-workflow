import json
import subprocess

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


def _task():
    from scripts.lib.task_spec import TaskSpec

    return TaskSpec.from_workflow_dispatch(
        {
            "app": "kvrocks",
            "version": "2.16.0",
            "os_version": "24.03-lts-sp4",
            "domain": "Database",
            "source_url": "https://github.com/apache/kvrocks/tree/v2.16.0",
        }
    )


def _candidate(tmp_path, upstream):
    from scripts.lib.candidate_bundle import CandidateBundle
    from scripts.lib.git_workspace import TargetWorkspace

    generated = TargetWorkspace.clone(
        str(upstream),
        tmp_path / "generated",
        branch="master",
    )
    image_dir = (
        generated.path
        / "Database"
        / "kvrocks"
        / "2.16.0"
        / "24.03-lts-sp4"
    )
    image_dir.mkdir(parents=True)
    (image_dir / "Dockerfile").write_text("FROM scratch\n")
    candidate = tmp_path / "candidate"
    (candidate / "reports").mkdir(parents=True)
    generated.create_patch(candidate / "changes.patch")
    for report in ("x86_64", "aarch64", "gates"):
        (candidate / "reports" / f"{report}.json").write_text(
            json.dumps({"status": "passed"}) + "\n"
        )
    CandidateBundle.create(
        candidate,
        task=_task(),
        base_sha=generated.base_sha,
        validated_run_id="123456",
    )
    return candidate, generated.base_sha


def test_promotes_exact_candidate_to_one_bot_commit_on_deterministic_branch(
    tmp_path,
):
    from scripts.lib.candidate_bundle import promote_candidate
    from scripts.lib.git_workspace import TargetWorkspace

    upstream = _upstream(tmp_path)
    candidate, base_sha = _candidate(tmp_path, upstream)
    workspace = TargetWorkspace.clone(
        str(upstream),
        tmp_path / "promotion",
        branch="master",
    )

    result = promote_candidate(
        candidate_dir=candidate,
        expected_run_id="123456",
        workspace=workspace,
    )

    assert result.branch == _task().branch
    assert result.base_sha == base_sha
    assert result.candidate_sha256
    assert _git(workspace.path, "branch", "--show-current") == _task().branch
    assert _git(workspace.path, "rev-parse", "HEAD^") == base_sha
    assert _git(workspace.path, "rev-parse", "HEAD") == result.commit_sha
    assert _git(workspace.path, "show", "-s", "--format=%an", "HEAD") == (
        "openEuler Docker Autopilot Bot"
    )
    assert _git(workspace.path, "show", "-s", "--format=%ae", "HEAD") == (
        "jcccx.cmh@gmail.com"
    )
    assert _git(workspace.path, "status", "--porcelain") == ""
    assert (
        workspace.path
        / "Database"
        / "kvrocks"
        / "2.16.0"
        / "24.03-lts-sp4"
        / "Dockerfile"
    ).read_text() == "FROM scratch\n"


def test_run_id_mismatch_fails_before_patch_is_applied(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundleError
    from scripts.lib.candidate_bundle import promote_candidate
    from scripts.lib.git_workspace import TargetWorkspace

    upstream = _upstream(tmp_path)
    candidate, base_sha = _candidate(tmp_path, upstream)
    workspace = TargetWorkspace.clone(
        str(upstream),
        tmp_path / "promotion",
        branch="master",
    )

    with pytest.raises(CandidateBundleError, match="run ID"):
        promote_candidate(
            candidate_dir=candidate,
            expected_run_id="999999",
            workspace=workspace,
        )

    assert _git(workspace.path, "rev-parse", "HEAD") == base_sha
    assert _git(workspace.path, "status", "--porcelain") == ""


def test_changed_target_base_requires_new_validation_run(tmp_path):
    from scripts.lib.candidate_bundle import (
        CandidatePromotionError,
        promote_candidate,
    )
    from scripts.lib.git_workspace import TargetWorkspace

    upstream = _upstream(tmp_path)
    candidate, old_base_sha = _candidate(tmp_path, upstream)
    (upstream / "README.md").write_text("upstream changed\n")
    _git(upstream, "add", "README.md")
    _git(upstream, "commit", "-m", "move target base")
    new_base_sha = _git(upstream, "rev-parse", "HEAD")
    workspace = TargetWorkspace.clone(
        str(upstream),
        tmp_path / "promotion",
        branch="master",
    )

    with pytest.raises(CandidatePromotionError, match="validate_only"):
        promote_candidate(
            candidate_dir=candidate,
            expected_run_id="123456",
            workspace=workspace,
        )

    assert old_base_sha != new_base_sha
    assert _git(workspace.path, "rev-parse", "HEAD") == new_base_sha
    assert _git(workspace.path, "status", "--porcelain") == ""
