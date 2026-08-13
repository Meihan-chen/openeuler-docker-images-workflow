import subprocess
import json
import xml.etree.ElementTree as ET
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


def _valid_results(repo: Path):
    task = _task()
    root = repo / task.mdu_path / "results" / task.version / task.os_version
    root.mkdir(parents=True)
    for architecture in task.architectures:
        suite = ET.Element(
            "testsuite", {"tests": "1", "failures": "0", "errors": "0"}
        )
        ET.SubElement(suite, "testcase", {"name": architecture})
        ET.ElementTree(suite).write(root / f"{architecture}.junit.xml")
    version_info = {
        "test_time": "2026-08-13T00:00:00Z",
        "Model": "native runners",
        "architecture": ",".join(task.architectures),
        "kernel": "per-architecture evidence",
        "os": "openEuler 26.03 LTS",
        "cpu_model": "per-architecture evidence",
        "cpu_cores": 16,
        "software_name": task.app,
        "software_version": task.version,
        "python_version": "not-installed",
        "numpy_version": "not-installed",
    }
    (root / "version_info.json").write_text(json.dumps(version_info))
    (root / "validation-summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "passed",
                "task_id": task.task_id,
                "task_key": task.task_key,
                "validated_run_id": "123",
                "architectures": list(task.architectures),
                "checks": ["native_build", "os_identity", "runtime_test"],
            }
        )
    )


def test_add_version_contract_accepts_target_directory_and_meta_append(tmp_path):
    from scripts.lib.target_contract import validate_add_version_target

    repo, base_sha = _repo(tmp_path)
    _valid_candidate(repo)

    report = validate_add_version_target(repo=repo, task=_task(), base_sha=base_sha)

    assert report["status"] == "passed"
    assert report["delivery_allowed"] is True
    assert report["modified_files"] == ["Database/redis/meta.yml"]


def test_final_add_version_contract_validates_summary_identity(tmp_path):
    from scripts.lib.target_contract import validate_final_target

    repo, base_sha = _repo(tmp_path)
    _valid_candidate(repo)
    _valid_results(repo)

    report = validate_final_target(repo=repo, task=_task(), base_sha=base_sha)

    assert report["status"] == "passed"


def test_final_add_version_contract_rejects_forged_summary(tmp_path):
    from scripts.lib.target_contract import TargetContractError, validate_final_target

    repo, base_sha = _repo(tmp_path)
    _valid_candidate(repo)
    _valid_results(repo)
    summary = repo / "Database/redis/results/8.2.1/26.03-lts/validation-summary.json"
    payload = json.loads(summary.read_text())
    payload["task_key"] = "0" * 16
    summary.write_text(json.dumps(payload))

    with pytest.raises(TargetContractError, match="validation-summary"):
        validate_final_target(repo=repo, task=_task(), base_sha=base_sha)


def test_add_version_contract_accepts_one_readme_row_inside_existing_table(tmp_path):
    from scripts.lib.target_contract import validate_add_version_target

    repo, _ = _repo(tmp_path)
    readme = repo / "Database" / "redis" / "README.md"
    readme.write_text(
        "intro\n"
        "| Tags | Currently | Architectures |\n"
        "|---|---|---|\n"
        "| [8.2.1-oe2403sp1](8.2.1/24.03-lts-sp1/Dockerfile) | "
        "redis 8.2.1 on openEuler 24.03-LTS-SP1 | amd64, arm64 |\n"
        "\nusage\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add readme")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _valid_candidate(repo)
    lines = readme.read_text().splitlines(keepends=True)
    lines.insert(
        4,
        "| [8.2.1-oe2603lts](8.2.1/26.03-lts/Dockerfile) | "
        "redis 8.2.1 on openEuler 26.03-lts | amd64, arm64 |\n",
    )
    readme.write_text("".join(lines))

    report = validate_add_version_target(repo=repo, task=_task(), base_sha=base_sha)

    assert report["status"] == "passed"
    assert report["modified_files"] == [
        "Database/redis/README.md",
        "Database/redis/meta.yml",
    ]


def test_add_version_contract_allows_publisher_readme_repair_with_target_row(tmp_path):
    from scripts.lib.target_contract import validate_add_version_target

    repo, _ = _repo(tmp_path)
    readme = repo / "Database" / "redis" / "README.md"
    readme.write_text("intro\n| old | row | here |\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add readme")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _valid_candidate(repo)
    readme.write_text(
        "changed intro\n"
        "| old | row | here |\n"
        "| 8.2.1-oe2603lts | 26.03-lts | "
        "8.2.1/26.03-lts/Dockerfile |\n"
    )

    report = validate_add_version_target(repo=repo, task=_task(), base_sha=base_sha)

    assert report["status"] == "passed"
    assert "Database/redis/README.md" in report["modified_files"]


def test_add_version_contract_rejects_removing_published_readme_rows(tmp_path):
    from scripts.lib.target_contract import TargetContractError, validate_add_version_target

    repo, _ = _repo(tmp_path)
    readme = repo / "Database" / "redis" / "README.md"
    readme.write_text(
        "| Tags | openEuler | Dockerfile |\n"
        "|---|---|---|\n"
        "| 7.4.1-oe2203sp3 | 22.03-lts-sp4 | "
        "/7.4.1/22.03-lts-sp4/Dockerfile |\n"
        "| 8.2.1-oe2403sp1 | 24.03-lts-sp1 | "
        "8.2.1/24.03-lts-sp1/Dockerfile |\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add published readme rows")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _valid_candidate(repo)
    readme.write_text(
        "| Tags | openEuler | Dockerfile |\n"
        "|---|---|---|\n"
        "| 8.2.1-oe2603lts | 26.03-lts | "
        "8.2.1/26.03-lts/Dockerfile |\n"
    )

    with pytest.raises(TargetContractError, match="published README rows"):
        validate_add_version_target(repo=repo, task=_task(), base_sha=base_sha)


def test_add_version_contract_allows_tests_and_docs_to_be_repaired(tmp_path):
    from scripts.lib.target_contract import validate_add_version_target

    repo, _ = _repo(tmp_path)
    tests = repo / "Database" / "redis" / "tests"
    tests.mkdir()
    test = tests / "test.sh"
    test.write_text("#!/bin/bash\nset -euo pipefail\nredis-cli ping\n")
    test.chmod(0o755)
    doc = repo / "Database" / "redis" / "doc" / "image-info.yml"
    doc.parent.mkdir()
    doc.write_text("name: redis\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add tests and docs")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _valid_candidate(repo)
    test.write_text("#!/bin/bash\nset -euo pipefail\nredis-cli set smoke ok\n")
    doc.write_text("name: redis\ndescription: Publisher repair\n")
    (doc.parent / "usage.md").write_text("# Redis usage\n")

    report = validate_add_version_target(repo=repo, task=_task(), base_sha=base_sha)

    assert report["status"] == "passed"
    assert report["modified_files"] == [
        "Database/redis/doc/image-info.yml",
        "Database/redis/meta.yml",
        "Database/redis/tests/test.sh",
    ]


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

    with pytest.raises(TargetContractError, match="preserve all published entries"):
        validate_add_version_target(repo=repo, task=_task(), base_sha=base_sha)


def test_add_version_contract_rejects_image_list_change(tmp_path):
    from scripts.lib.target_contract import TargetContractError, validate_add_version_target

    repo, base_sha = _repo(tmp_path)
    _valid_candidate(repo)
    with (repo / "Database" / "image-list.yml").open("a") as stream:
        stream.write("  mysql: mysql\n")

    with pytest.raises(TargetContractError, match="outside add-version scope"):
        validate_add_version_target(repo=repo, task=_task(), base_sha=base_sha)


@pytest.mark.parametrize(
    "relative",
    (
        "8.2.1/26.03-lts/AGENTS.md",
        "tests/.agents/runtime.md",
        "doc/.codex/settings.json",
        "doc/CLAUDE.md",
    ),
)
def test_add_version_contract_rejects_new_agent_controls(tmp_path, relative):
    from scripts.lib.target_contract import TargetContractError, validate_add_version_target

    repo, base_sha = _repo(tmp_path)
    _valid_candidate(repo)
    control = repo / "Database" / "redis" / relative
    control.parent.mkdir(parents=True, exist_ok=True)
    control.write_text("agent override\n")

    with pytest.raises(TargetContractError, match="Agent control file"):
        validate_add_version_target(repo=repo, task=_task(), base_sha=base_sha)
