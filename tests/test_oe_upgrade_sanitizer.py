import json
import stat
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


def _candidate(tmp_path: Path):
    from scripts.lib.oe_upgrade_candidate import prepare_upgrade_candidate

    repo = tmp_path / "target"
    source = repo / "Database" / "redis" / "8.2.1" / "24.03-lts-sp1"
    source.mkdir(parents=True)
    (repo / "Database" / "image-list.yml").write_text("images:\n  redis: redis\n")
    (repo / "Database" / "redis" / "meta.yml").write_text(
        "8.2.1-oe2403sp1:\n  path: 8.2.1/24.03-lts-sp1/Dockerfile\n"
    )
    (source / "Dockerfile").write_text(
        "ARG BASE=openeuler/openeuler:24.03-lts-sp1\n"
        "ARG REDIS_VERSION=8.2.1\n"
        "FROM $BASE\n"
        "RUN curl -L https://example.test/redis-8.2.1.tar.gz\n"
    )
    mysql = repo / "Database" / "mysql"
    mysql.mkdir()
    (mysql / "Dockerfile").write_text("FROM scratch\n")
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    base_sha = _git(repo, "rev-parse", "HEAD")
    prepare_upgrade_candidate(
        workspace=repo,
        task=_task(),
        base_sha=base_sha,
        report_dir=tmp_path / "derive",
    )
    return repo, base_sha


def test_checkpoint_captures_candidate_files_and_allowed_fixer_scope(tmp_path):
    from scripts.lib.oe_upgrade_sanitizer import create_checkpoint

    repo, base_sha = _candidate(tmp_path)
    checkpoint = create_checkpoint(
        workspace=repo,
        base_sha=base_sha,
        task=_task(),
        destination=tmp_path / "checkpoint",
        round_number=1,
        agent_role="code-fixer",
    )

    assert checkpoint.checkpoint_id.startswith("sha256:")
    assert checkpoint.allowed_paths == (
        "Database/redis/8.2.1/26.03-lts/**",
        "Database/redis/tests/**",
        "Database/redis/meta.yml",
        "Database/redis/README.md",
        "Database/redis/doc/**",
    )
    assert "Database/redis/meta.yml" in checkpoint.files
    assert "Database/redis/8.2.1/26.03-lts/Dockerfile" in checkpoint.files
    assert checkpoint.candidate_patch_sha256.startswith("sha256:")
    assert json.loads((tmp_path / "checkpoint" / "manifest.json").read_text())[
        "checkpoint_id"
    ] == checkpoint.checkpoint_id


def test_sanitizer_keeps_fixer_mdu_changes_and_restores_everything_else(tmp_path):
    from scripts.lib.oe_upgrade_sanitizer import create_checkpoint, sanitize_agent_changes

    repo, base_sha = _candidate(tmp_path)
    checkpoint = create_checkpoint(
        workspace=repo,
        base_sha=base_sha,
        task=_task(),
        destination=tmp_path / "checkpoint",
        round_number=1,
        agent_role="code-fixer",
    )
    target = repo / "Database" / "redis" / "8.2.1" / "26.03-lts" / "Dockerfile"
    target.write_text(target.read_text() + "RUN dnf -y install compat-lib\n")
    meta = repo / "Database" / "redis" / "meta.yml"
    meta.write_text("# Publisher-normalized metadata\n" + meta.read_text())
    readme = repo / "Database" / "redis" / "README.md"
    readme.write_text(
        "| Tags | openEuler | Dockerfile |\n"
        "|---|---|---|\n"
        "| 8.2.1-oe2603lts | 26.03-lts | "
        "8.2.1/26.03-lts/Dockerfile |\n"
    )
    test = repo / "Database" / "redis" / "tests" / "test.sh"
    test.parent.mkdir()
    test.write_text("#!/bin/bash\nset -euo pipefail\nredis-server --version\n")
    test.chmod(0o755)
    doc = repo / "Database" / "redis" / "doc" / "image-info.yml"
    doc.parent.mkdir()
    doc.write_text("name: redis\n")
    history = repo / "Database" / "redis" / "8.2.1" / "24.03-lts-sp1" / "Dockerfile"
    expected_history = history.read_bytes()
    history.write_text("FROM broken\n")
    mysql = repo / "Database" / "mysql" / "Dockerfile"
    mysql.write_text("FROM broken\n")
    unauthorized = repo / "Database" / "redis" / "notes.txt"
    unauthorized.write_text("remove me\n")

    report = sanitize_agent_changes(
        workspace=repo,
        base_sha=base_sha,
        task=_task(),
        checkpoint=checkpoint,
        report_path=tmp_path / "sanitization.json",
    )

    assert "compat-lib" in target.read_text()
    assert meta.read_text().startswith("# Publisher-normalized metadata\n")
    assert readme.is_file()
    assert test.is_file()
    assert doc.is_file()
    assert history.read_bytes() == expected_history
    assert mysql.read_text() == "FROM scratch\n"
    assert not unauthorized.exists()
    assert report.clean is True
    assert report.retained_changes == tuple(
        sorted(
            (
                "Database/redis/8.2.1/26.03-lts/Dockerfile",
                "Database/redis/README.md",
                "Database/redis/doc/image-info.yml",
                "Database/redis/meta.yml",
                "Database/redis/tests/test.sh",
            )
        )
    )
    actions = {item["path"]: item["action"] for item in report.actions}
    assert actions["Database/mysql/Dockerfile"] == "restore-base"
    assert actions["Database/redis/notes.txt"] == "remove-unauthorized"


def test_sanitizer_removes_agent_controls_even_inside_fixer_scope(tmp_path):
    from scripts.lib.oe_upgrade_sanitizer import create_checkpoint, sanitize_agent_changes

    repo, base_sha = _candidate(tmp_path)
    checkpoint = create_checkpoint(
        workspace=repo,
        base_sha=base_sha,
        task=_task(),
        destination=tmp_path / "checkpoint",
        round_number=1,
        agent_role="code-fixer",
    )
    controls = (
        repo
        / "Database"
        / "redis"
        / "8.2.1"
        / "26.03-lts"
        / ".codex"
        / "config.json",
        repo / "Database" / "redis" / "doc" / "AGENTS.md",
        repo / "Database" / "redis" / "tests" / "CLAUDE.md",
    )
    for control in controls:
        control.parent.mkdir(parents=True, exist_ok=True)
        control.write_text("agent override\n")
    normal_doc = repo / "Database" / "redis" / "doc" / "usage.md"
    normal_doc.write_text("# Redis\n")

    report = sanitize_agent_changes(
        workspace=repo,
        base_sha=base_sha,
        task=_task(),
        checkpoint=checkpoint,
        report_path=tmp_path / "sanitization.json",
    )

    assert all(not path.exists() for path in controls)
    assert normal_doc.is_file()
    assert report.retained_changes == ("Database/redis/doc/usage.md",)
    actions = {item["path"]: item["action"] for item in report.actions}
    assert actions[
        "Database/redis/8.2.1/26.03-lts/.codex/config.json"
    ] == "remove-unauthorized"
    assert actions["Database/redis/doc/AGENTS.md"] == "remove-unauthorized"
    assert actions["Database/redis/tests/CLAUDE.md"] == "remove-unauthorized"


def test_testcase_creator_can_only_add_tests(tmp_path):
    from scripts.lib.oe_upgrade_sanitizer import create_checkpoint, sanitize_agent_changes

    repo, base_sha = _candidate(tmp_path)
    checkpoint = create_checkpoint(
        workspace=repo,
        base_sha=base_sha,
        task=_task(),
        destination=tmp_path / "checkpoint",
        round_number=0,
        agent_role="testcase-creator",
    )
    test = repo / "Database" / "redis" / "tests" / "test.sh"
    test.parent.mkdir()
    test.write_text("#!/bin/bash\nset -euo pipefail\nredis-server --version\n")
    test.chmod(0o755)
    target = repo / "Database" / "redis" / "8.2.1" / "26.03-lts" / "Dockerfile"
    expected_target = target.read_bytes()
    target.write_text("FROM scratch\n")

    report = sanitize_agent_changes(
        workspace=repo,
        base_sha=base_sha,
        task=_task(),
        checkpoint=checkpoint,
        report_path=tmp_path / "sanitization.json",
    )

    assert test.is_file()
    assert stat.S_IMODE(test.stat().st_mode) == 0o755
    assert target.read_bytes() == expected_target
    assert report.retained_changes == ("Database/redis/tests/test.sh",)


def test_sanitizer_rejects_tampered_checkpoint_manifest(tmp_path):
    from scripts.lib.oe_upgrade_sanitizer import (
        SanitizationError,
        create_checkpoint,
        load_checkpoint,
    )

    repo, base_sha = _candidate(tmp_path)
    create_checkpoint(
        workspace=repo,
        base_sha=base_sha,
        task=_task(),
        destination=tmp_path / "checkpoint",
        round_number=1,
        agent_role="code-fixer",
    )
    manifest = tmp_path / "checkpoint" / "manifest.json"
    payload = json.loads(manifest.read_text())
    payload["round"] = 99
    manifest.write_text(json.dumps(payload))

    with pytest.raises(SanitizationError, match="checkpoint_id"):
        load_checkpoint(tmp_path / "checkpoint")


def test_sanitizer_hard_stops_when_fixer_changes_source_identity(tmp_path):
    from scripts.lib.oe_upgrade_sanitizer import (
        SanitizationError,
        create_checkpoint,
        sanitize_agent_changes,
    )

    repo, base_sha = _candidate(tmp_path)
    checkpoint = create_checkpoint(
        workspace=repo,
        base_sha=base_sha,
        task=_task(),
        destination=tmp_path / "checkpoint",
        round_number=1,
        agent_role="code-fixer",
    )
    target = repo / "Database" / "redis" / "8.2.1" / "26.03-lts" / "Dockerfile"
    target.write_text(target.read_text().replace("redis-8.2.1", "redis-9.0.0"))

    with pytest.raises(SanitizationError, match="source identity"):
        sanitize_agent_changes(
            workspace=repo,
            base_sha=base_sha,
            task=_task(),
            checkpoint=checkpoint,
            report_path=tmp_path / "sanitization.json",
        )
