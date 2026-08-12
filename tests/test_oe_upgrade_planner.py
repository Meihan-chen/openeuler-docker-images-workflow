import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import yaml


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _commit_fixture(repo: Path) -> str:
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return _git(repo, "rev-parse", "HEAD")


def _request(base_sha: str, *scope: str):
    from scripts.lib.oe_upgrade_contract import UpgradeRequest

    return UpgradeRequest.create(
        tracking_issue_number=123,
        oe_version="26.03-lts",
        scope=scope or ("Database",),
        base_sha=base_sha,
    )


def _write_mdu(
    repo: Path,
    *,
    domain: str,
    image_name: str,
    relative_path: str,
    meta: dict,
):
    image_list = repo / domain / "image-list.yml"
    image_list.parent.mkdir(parents=True, exist_ok=True)
    existing = yaml.safe_load(image_list.read_text()) if image_list.exists() else {}
    existing = existing or {}
    existing.setdefault("images", {})[image_name] = relative_path
    image_list.write_text(yaml.safe_dump(existing, sort_keys=False))
    mdu = repo / domain / relative_path
    mdu.mkdir(parents=True, exist_ok=True)
    (mdu / "meta.yml").write_text(yaml.safe_dump(meta, sort_keys=False))
    for value in meta.values():
        path = value.get("path") if isinstance(value, dict) else None
        if not isinstance(path, str) or path.startswith("/"):
            continue
        parts = Path(path).parts
        if len(parts) != 3 or parts[0] == domain:
            continue
        dockerfile = mdu / path
        dockerfile.parent.mkdir(parents=True, exist_ok=True)
        dockerfile.write_text("FROM openeuler/openeuler:24.03-lts-sp4\n")


def test_planner_keeps_redis_when_historical_entries_are_invalid(tmp_path):
    from scripts.lib.oe_upgrade_planner import plan_upgrade

    _write_mdu(
        tmp_path,
        domain="Database",
        image_name="redis",
        relative_path="redis",
        meta={
            "7.4.1-oe2203sp3": {
                "path": "/7.4.1/22.03-lts-sp4/Dockerfile"
            },
            "8.0.2-oe2403sp1": {
                "path": "Database/redis/8.0.2/24.03-lts-sp1/Dockerfile"
            },
            "8.2.1-oe2403sp1": {
                "path": "8.2.1/24.03-lts-sp1/Dockerfile"
            },
            "8.6.4-oe2403sp3": {
                "path": "5.4.1/24.03-lts-sp3/Dockerfile"
            },
        },
    )
    base_sha = _commit_fixture(tmp_path)

    plan = plan_upgrade(tmp_path, _request(base_sha))

    assert plan.summary == {
        "mdu_count": 1,
        "task_count": 1,
        "planning_failed_count": 0,
        "warning_count": 3,
    }
    task = plan.tasks[0]
    assert task.mdu_path == "Database/redis"
    assert task.version == "8.2.1"
    assert task.derive_from == "8.2.1/24.03-lts-sp1"
    assert task.architectures == ("x86_64", "aarch64")
    assert {warning["reason"] for warning in plan.warnings} == {
        "absolute meta path rejected",
        "repository-relative meta path rejected",
        "tag application version does not match meta path",
    }


def test_planner_discovers_nested_mdu_from_image_list(tmp_path):
    from scripts.lib.oe_upgrade_planner import plan_upgrade

    _write_mdu(
        tmp_path,
        domain="AI",
        image_name="kserve-agent",
        relative_path="kserve/agent",
        meta={
            "0.15.2-oe2403lts": {
                "path": "0.15.2/24.03-lts/Dockerfile",
                "arch": "x86_64",
            }
        },
    )
    base_sha = _commit_fixture(tmp_path)

    plan = plan_upgrade(tmp_path, _request(base_sha, "AI"))

    assert [task.mdu_path for task in plan.tasks] == ["AI/kserve/agent"]
    assert plan.tasks[0].architectures == ("x86_64",)


def test_planner_reports_missing_meta_and_orphan_meta_separately(tmp_path):
    from scripts.lib.oe_upgrade_planner import plan_upgrade

    (tmp_path / "Cloud").mkdir()
    (tmp_path / "Cloud" / "image-list.yml").write_text("images:\n  kolla: kolla\n")
    orphan = tmp_path / "Cloud" / "orphan"
    orphan.mkdir()
    (orphan / "meta.yml").write_text("{}\n")
    base_sha = _commit_fixture(tmp_path)

    plan = plan_upgrade(tmp_path, _request(base_sha, "Cloud"))

    assert plan.summary["mdu_count"] == 1
    assert plan.planning_failures == (
        {"mdu_path": "Cloud/kolla", "reason": "meta.yml is missing"},
    )
    assert plan.warnings == (
        {
            "mdu_path": "Cloud/orphan",
            "entry": "",
            "path": "Cloud/orphan/meta.yml",
            "reason": "meta.yml is not indexed by image-list.yml",
        },
    )


def test_planner_rejects_uncomparable_oceanbase_versions(tmp_path):
    from scripts.lib.oe_upgrade_planner import plan_upgrade

    _write_mdu(
        tmp_path,
        domain="Database",
        image_name="oceanbase",
        relative_path="oceanbase",
        meta={
            "4.3.5_CE_BP2_HF1-oe2403sp1": {
                "path": "4.3.5_CE_BP2_HF1/24.03-lts-sp1/Dockerfile"
            },
            "4.3.5_CE_BP3-oe2403sp4": {
                "path": "4.3.5_CE_BP3/24.03-lts-sp4/Dockerfile"
            },
        },
    )
    base_sha = _commit_fixture(tmp_path)

    plan = plan_upgrade(tmp_path, _request(base_sha))

    assert plan.tasks == ()
    assert plan.planning_failures == (
        {
            "mdu_path": "Database/oceanbase",
            "reason": "no comparable stable application version",
        },
    )


def test_planner_uses_fixed_git_tree_not_dirty_worktree(tmp_path):
    from scripts.lib.oe_upgrade_planner import plan_upgrade

    _write_mdu(
        tmp_path,
        domain="Database",
        image_name="redis",
        relative_path="redis",
        meta={
            "8.2.1-oe2403sp1": {
                "path": "8.2.1/24.03-lts-sp1/Dockerfile"
            }
        },
    )
    base_sha = _commit_fixture(tmp_path)
    shutil.rmtree(tmp_path / "Database" / "redis" / "8.2.1")
    (tmp_path / "Database" / "redis" / "meta.yml").write_text("{}\n")

    plan = plan_upgrade(tmp_path, _request(base_sha))

    assert plan.tasks[0].version == "8.2.1"
    assert hashlib.sha256(plan.to_json().encode()).hexdigest() == hashlib.sha256(
        plan_upgrade(tmp_path, _request(base_sha)).to_json().encode()
    ).hexdigest()
    assert json.loads(plan.to_json())["summary"]["task_count"] == 1
