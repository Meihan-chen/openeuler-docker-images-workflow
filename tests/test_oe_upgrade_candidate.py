import json
import os
import stat
import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _task(**overrides):
    from scripts.lib.task_spec import TaskSpec

    raw = {
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
    raw.update(overrides)
    return TaskSpec.from_workflow_dispatch(raw)


def _repo(tmp_path: Path, dockerfile: str, *, readme: str | None = None):
    repo = tmp_path / "target"
    source = repo / "Database" / "redis" / "8.2.1" / "24.03-lts-sp1"
    source.mkdir(parents=True)
    (repo / "Database" / "image-list.yml").write_text(
        "images:\n  redis: redis\n"
    )
    (repo / "Database" / "redis" / "meta.yml").write_text(
        "8.2.1-oe2403sp1:\n"
        "  path: 8.2.1/24.03-lts-sp1/Dockerfile\n"
    )
    (source / "Dockerfile").write_text(dockerfile)
    helper = source / "entrypoint.sh"
    helper.write_text("#!/bin/sh\nexec redis-server\n")
    helper.chmod(0o755)
    (source / "redis.conf").write_text("bind 0.0.0.0\n")
    if readme is not None:
        (repo / "Database" / "redis" / "README.md").write_text(readme)
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_prepare_candidate_copies_tree_preserves_mode_and_rewrites_base_arg(tmp_path):
    from scripts.lib.oe_upgrade_candidate import prepare_upgrade_candidate

    repo, base_sha = _repo(
        tmp_path,
        "# keep openeuler/openeuler:24.03-lts-sp1 in comment\n"
        "ARG BASE=openeuler/openeuler:24.03-lts-sp1\n"
        "ARG REDIS_VERSION=8.2.1\n"
        "FROM $BASE\n"
        "RUN curl -L https://example.test/redis-8.2.1.tar.gz\n",
    )

    report = prepare_upgrade_candidate(
        workspace=repo,
        task=_task(),
        base_sha=base_sha,
        report_dir=tmp_path / "reports",
    )

    target = repo / "Database" / "redis" / "8.2.1" / "26.03-lts"
    content = (target / "Dockerfile").read_text()
    assert "ARG BASE=openeuler/openeuler:26.03-lts" in content
    assert "# keep openeuler/openeuler:24.03-lts-sp1 in comment" in content
    assert "redis-8.2.1.tar.gz" in content
    assert stat.S_IMODE((target / "entrypoint.sh").stat().st_mode) == 0o755
    assert (target / "redis.conf").read_text() == "bind 0.0.0.0\n"
    assert report.source_directory.endswith("8.2.1/24.03-lts-sp1")
    assert report.target_directory.endswith("8.2.1/26.03-lts")
    assert report.copied_files == ("Dockerfile", "entrypoint.sh", "redis.conf")
    assert len(report.dockerfile_rewrites) == 1


def test_dockerfile_rewriter_handles_direct_variable_and_multistage_forms():
    from scripts.lib.oe_upgrade_candidate import rewrite_dockerfile_base

    original = (
        "ARG OE_VERSION=24.03-lts-sp1\n"
        "FROM --platform=$TARGETPLATFORM openeuler/openeuler:${OE_VERSION} AS build\n"
        "RUN echo openeuler/openeuler:24.03-lts-sp1\n"
        "FROM openeuler/openeuler:24.03-lts-sp1\n"
        "FROM openeuler/distroless-static:redis-8.2.1-oe2403sp1 AS final\n"
    )

    rewritten, records = rewrite_dockerfile_base(
        original,
        source_oe="24.03-lts-sp1",
        target_oe="26.03-lts",
        relative_path="Dockerfile",
    )

    assert "ARG OE_VERSION=26.03-lts" in rewritten
    assert "FROM openeuler/openeuler:26.03-lts" in rewritten
    assert "RUN echo openeuler/openeuler:24.03-lts-sp1" in rewritten
    assert "openeuler/distroless-static:redis-8.2.1-oe2403sp1" in rewritten
    assert len(records) == 2


def test_prepare_candidate_appends_meta_without_reformatting_history(tmp_path):
    from scripts.lib.oe_upgrade_candidate import prepare_upgrade_candidate

    repo, base_sha = _repo(
        tmp_path,
        "ARG BASE=openeuler/openeuler:24.03-lts-sp1\nFROM ${BASE}\n",
    )
    meta = repo / "Database" / "redis" / "meta.yml"
    before = meta.read_bytes()

    report = prepare_upgrade_candidate(
        workspace=repo,
        task=_task(),
        base_sha=base_sha,
        report_dir=tmp_path / "reports",
    )

    after = meta.read_bytes()
    assert after.startswith(before)
    assert after[len(before) :] == (
        b"8.2.1-oe2603lts:\n  path: 8.2.1/26.03-lts/Dockerfile\n"
    )
    assert report.meta_entry == {
        "tag": "8.2.1-oe2603lts",
        "path": "8.2.1/26.03-lts/Dockerfile",
        "architectures": ["x86_64", "aarch64"],
    }
    assert report.readme == {
        "path": "Database/redis/README.md",
        "row_added": False,
        "reason": "readme-update-skipped: file is missing",
    }


def test_prepare_candidate_appends_readme_row_from_unique_source_row(tmp_path):
    from scripts.lib.oe_upgrade_candidate import prepare_upgrade_candidate

    readme = (
        "| Image | openEuler | Dockerfile |\n"
        "| --- | --- | --- |\n"
        "| openeuler/redis:8.2.1-oe2403sp1 | 24.03-lts-sp1 | "
        "[Dockerfile](8.2.1/24.03-lts-sp1/Dockerfile) |\n"
    )
    repo, base_sha = _repo(
        tmp_path,
        "ARG BASE=openeuler/openeuler:24.03-lts-sp1\nFROM $BASE\n",
        readme=readme,
    )

    report = prepare_upgrade_candidate(
        workspace=repo,
        task=_task(),
        base_sha=base_sha,
        report_dir=tmp_path / "reports",
    )

    content = (repo / "Database" / "redis" / "README.md").read_text()
    assert content.startswith(readme)
    assert (
        "| openeuler/redis:8.2.1-oe2603lts | 26.03-lts | "
        "[Dockerfile](8.2.1/26.03-lts/Dockerfile) |"
    ) in content
    assert report.readme["row_added"] is True


def test_prepare_candidate_rejects_symlink_in_source_tree(tmp_path):
    from scripts.lib.oe_upgrade_candidate import CandidateDerivationError, prepare_upgrade_candidate

    repo, _ = _repo(
        tmp_path,
        "ARG BASE=openeuler/openeuler:24.03-lts-sp1\nFROM $BASE\n",
    )
    source = repo / "Database" / "redis" / "8.2.1" / "24.03-lts-sp1"
    os.symlink("redis.conf", source / "linked.conf")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add link")
    base_sha = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(CandidateDerivationError, match="symbolic link"):
        prepare_upgrade_candidate(
            workspace=repo,
            task=_task(),
            base_sha=base_sha,
            report_dir=tmp_path / "reports",
        )


def test_prepare_candidate_report_is_serializable(tmp_path):
    from scripts.lib.oe_upgrade_candidate import prepare_upgrade_candidate

    repo, base_sha = _repo(
        tmp_path,
        "ARG BASE=openeuler/openeuler:24.03-lts-sp1\nFROM $BASE\n",
    )
    report_dir = tmp_path / "reports"
    report = prepare_upgrade_candidate(
        workspace=repo,
        task=_task(),
        base_sha=base_sha,
        report_dir=report_dir,
    )

    payload = json.loads((report_dir / "derivation-report.json").read_text())
    assert payload == report.to_dict()
    assert payload["source_tree_sha256"].startswith("sha256:")
    assert payload["source_identity"]["app_version"] == "8.2.1"
