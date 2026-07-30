import subprocess


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


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


def _target(tmp_path):
    repo = tmp_path / "target"
    subprocess.run(
        ["git", "init", "-b", "master", str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.com")
    (repo / "Database").mkdir()
    (repo / "Database" / "image-list.yml").write_text(
        "images:\n  redis: redis\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_deterministic_smoke_candidate_passes_the_real_target_contract(
    tmp_path,
):
    from scripts.lib.generation_pipeline import write_smoke_candidate
    from scripts.lib.target_contract import validate_generated_target

    repo, base_sha = _target(tmp_path)

    result = write_smoke_candidate(workspace=repo, task=_task())
    gate = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert result["status"] == "passed"
    assert result["mode"] == "pipeline_smoke"
    assert gate["status"] == "passed"
    dockerfile = (
        repo
        / "Database"
        / "kvrocks"
        / "2.16.0"
        / "24.03-lts-sp4"
        / "Dockerfile"
    )
    assert dockerfile.is_file()
    assert not (dockerfile.parent / "test.sh").exists()
    shared_test = repo / "Database" / "kvrocks" / "tests" / "test.sh"
    assert shared_test.is_file()
    assert shared_test.stat().st_mode & 0o111
    dockerfile_text = dockerfile.read_text()
    assert "dnf install -y redis" in dockerfile_text
    assert "dnf clean all" in dockerfile_text
    assert "groupadd --non-unique --gid 999" in dockerfile_text
    assert "useradd --non-unique --uid 999" in dockerfile_text
