import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()


def _task():
    from scripts.lib.task_spec import TaskSpec

    return TaskSpec.from_workflow_dispatch(
        {
            "schema_version": 2,
            "scenario": "oe-upgrade",
            "app": "redis",
            "image_name": "redis",
            "version": "8.2.1",
            "os_version": "26.03-lts",
            "domain": "Database",
            "source_url": "",
            "mdu_path": "Database/redis",
            "derive_from": "8.2.1/24.03-lts-sp1",
            "architectures": ["x86_64", "aarch64"],
        }
    )


def _repo(tmp_path: Path):
    repo = tmp_path / "target"
    source = repo / "Database" / "redis" / "8.2.1" / "24.03-lts-sp1"
    source.mkdir(parents=True)
    (repo / "Database" / "image-list.yml").write_text("images:\n  redis: redis\n")
    (repo / "Database" / "redis" / "meta.yml").write_text(
        "7.4.1-oe2203sp3:\n"
        "  path: /7.4.1/22.03-lts-sp4/Dockerfile\n"
        "8.2.1-oe2403sp1:\n"
        "  path: 8.2.1/24.03-lts-sp1/Dockerfile\n"
    )
    (source / "Dockerfile").write_text(
        "ARG BASE=openeuler/openeuler:24.03-lts-sp1\nFROM $BASE\n"
    )
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _valid_candidate(repo: Path):
    target = repo / "Database" / "redis" / "8.2.1" / "26.03-lts"
    target.mkdir()
    (target / "Dockerfile").write_text(
        "ARG BASE=openeuler/openeuler:26.03-lts\nFROM $BASE\n"
    )
    meta = repo / "Database" / "redis" / "meta.yml"
    with meta.open("a") as stream:
        stream.write(
            "8.2.1-oe2603lts:\n  path: 8.2.1/26.03-lts/Dockerfile\n"
        )


def test_add_version_contract_accepts_target_directory_and_meta_append(tmp_path):
    from scripts.lib.target_contract import validate_add_version_target

    repo, base_sha = _repo(tmp_path)
    _valid_candidate(repo)

    report = validate_add_version_target(repo=repo, task=_task(), base_sha=base_sha)

    assert report["status"] == "passed"
    assert report["delivery_allowed"] is True
    assert report["modified_files"] == ["Database/redis/meta.yml"]


def test_add_version_contract_rejects_history_or_other_mdu_changes(tmp_path):
    from scripts.lib.target_contract import TargetContractError, validate_add_version_target

    repo, base_sha = _repo(tmp_path)
    _valid_candidate(repo)
    history = repo / "Database" / "redis" / "8.2.1" / "24.03-lts-sp1" / "Dockerfile"
    history.write_text("FROM scratch\n")
    other = repo / "Database" / "mysql"
    other.mkdir()
    (other / "notes.txt").write_text("unauthorized\n")

    with pytest.raises(TargetContractError, match="outside add-version scope"):
        validate_add_version_target(repo=repo, task=_task(), base_sha=base_sha)


def test_add_version_contract_rejects_meta_history_rewrite(tmp_path):
    from scripts.lib.target_contract import TargetContractError, validate_add_version_target

    repo, base_sha = _repo(tmp_path)
    _valid_candidate(repo)
    meta = repo / "Database" / "redis" / "meta.yml"
    meta.write_text(meta.read_text().replace("8.2.1-oe2403sp1", "8.2.0-oe2403sp1"))

    with pytest.raises(TargetContractError, match="append-only"):
        validate_add_version_target(repo=repo, task=_task(), base_sha=base_sha)


def test_add_version_contract_rejects_image_list_change(tmp_path):
    from scripts.lib.target_contract import TargetContractError, validate_add_version_target

    repo, base_sha = _repo(tmp_path)
    _valid_candidate(repo)
    with (repo / "Database" / "image-list.yml").open("a") as stream:
        stream.write("  mysql: mysql\n")

    with pytest.raises(TargetContractError, match="outside add-version scope"):
        validate_add_version_target(repo=repo, task=_task(), base_sha=base_sha)
