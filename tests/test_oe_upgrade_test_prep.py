import subprocess
from pathlib import Path

from scripts.lib.agent_runtime import AgentResult


COMMAND_EVIDENCE = [
    {
        "command": "application --version",
        "semantics": "prints the application version checked by test.sh",
        "evidence_id": "creator-command-001",
    }
]


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
            "app": "agent",
            "image_name": "kserve-agent",
            "version": "0.15.2",
            "os_version": "26.03-lts",
            "domain": "AI",
            "source_url": "",
            "mdu_path": "AI/kserve/agent",
            "derive_from": "0.15.2/24.03-lts",
            "architectures": ["x86_64"],
        }
    )


def _candidate(tmp_path: Path, *, test: bool):
    repo = tmp_path / "target"
    mdu = repo / "AI" / "kserve" / "agent"
    source = mdu / "0.15.2" / "24.03-lts"
    target = mdu / "0.15.2" / "26.03-lts"
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    (repo / "AI" / "image-list.yml").write_text(
        "images:\n  kserve-agent: kserve/agent\n"
    )
    (mdu / "meta.yml").write_text(
        "0.15.2-oe2403lts:\n"
        "  path: 0.15.2/24.03-lts/Dockerfile\n"
        "  arch: x86_64\n"
    )
    (source / "Dockerfile").write_text("FROM scratch\n")
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (target / "Dockerfile").write_text("FROM scratch\n")
    with (mdu / "meta.yml").open("a") as stream:
        stream.write(
            "0.15.2-oe2603lts:\n"
            "  path: 0.15.2/26.03-lts/Dockerfile\n"
            "  arch: x86_64\n"
        )
    if test:
        tests = mdu / "tests"
        tests.mkdir()
        entry = tests / "test.sh"
        entry.write_text("#!/bin/bash\nset -euo pipefail\ntrue\n")
        entry.chmod(0o755)
    return repo, base_sha


def test_existing_nested_mdu_test_is_reused_without_agent(tmp_path):
    from scripts.lib.oe_upgrade_test_prep import prepare_upgrade_tests

    repo, base_sha = _candidate(tmp_path, test=True)
    calls = []

    result = prepare_upgrade_tests(
        workspace=repo,
        task=_task(),
        base_sha=base_sha,
        checkpoint_dir=tmp_path / "checkpoint",
        report_path=tmp_path / "reports" / "sanitization.json",
        evidence_dir=tmp_path / "reports",
        agent_runner=lambda **kwargs: calls.append(kwargs) or None,
    )

    assert result.status == "reused-existing"
    assert result.reused_existing is True
    assert calls == []


def test_missing_test_runs_creator_then_read_only_qa(tmp_path):
    from scripts.lib.oe_upgrade_test_prep import prepare_upgrade_tests

    repo, base_sha = _candidate(tmp_path, test=False)
    roles = []
    qa_prompts = []

    def agent_runner(**kwargs):
        roles.append(kwargs["role"])
        if kwargs["role"] == "testcase_creator":
            test = repo / "AI" / "kserve" / "agent" / "tests" / "test.sh"
            test.parent.mkdir()
            test.write_text("#!/bin/bash\nset -euo pipefail\ntrue\n")
            test.chmod(0o755)
            return AgentResult(
                role="testcase_creator",
                payload={
                    "success": True,
                    "files_created": ["AI/kserve/agent/tests/test.sh"],
                    "command_evidence": COMMAND_EVIDENCE,
                },
            )
        qa_prompts.append(kwargs["prompt"])
        return AgentResult(
            role="testcase_qa",
            payload={
                "status": "approved",
                "issues": [],
                "coverage_score": 1.0,
                "summary": "ok",
            },
        )

    result = prepare_upgrade_tests(
        workspace=repo,
        task=_task(),
        base_sha=base_sha,
        checkpoint_dir=tmp_path / "checkpoint",
        report_path=tmp_path / "reports" / "sanitization.json",
        evidence_dir=tmp_path / "reports",
        executable=tmp_path / "opencode",
        api_key="secret",
        agent_runner=agent_runner,
    )

    assert result.status == "generated"
    assert roles == ["testcase_creator", "testcase_qa"]
    assert "`AI/kserve/agent/tests/test.sh`" in qa_prompts[0]
    assert "`AI/agent/tests/test.sh`" not in qa_prompts[0]
    assert result.sanitization is not None and result.sanitization.clean is True
    assert (tmp_path / "reports" / "testcase-creator.json").is_file()
    assert (tmp_path / "reports" / "testcase-qa-round1.json").is_file()


def test_existing_test_still_writes_approved_qa_evidence(tmp_path):
    from scripts.lib.oe_upgrade_test_prep import prepare_upgrade_tests

    repo, base_sha = _candidate(tmp_path, test=True)
    report_dir = tmp_path / "reports"
    result = prepare_upgrade_tests(
        workspace=repo,
        task=_task(),
        base_sha=base_sha,
        checkpoint_dir=tmp_path / "checkpoint",
        report_path=report_dir / "sanitization.json",
        evidence_dir=report_dir,
    )

    assert result.status == "reused-existing"
    assert (report_dir / "testcase-qa-round1.json").is_file()


def test_testcase_qa_requests_one_sanitized_creator_repair(tmp_path):
    from scripts.lib.oe_upgrade_test_prep import prepare_upgrade_tests

    repo, base_sha = _candidate(tmp_path, test=False)
    roles = []
    timeouts = []

    def agent_runner(**kwargs):
        roles.append(kwargs["role"])
        timeouts.append(kwargs["timeout"])
        test = repo / "AI" / "kserve" / "agent" / "tests" / "test.sh"
        if kwargs["role"] == "testcase_creator":
            test.parent.mkdir(exist_ok=True)
            test.write_text("#!/bin/bash\nset -euo pipefail\ntrue\n")
            test.chmod(0o755)
            return AgentResult(
                role="testcase_creator",
                payload={
                    "success": True,
                    "files_created": ["AI/kserve/agent/tests/test.sh"],
                    "command_evidence": COMMAND_EVIDENCE,
                },
            )
        qa_round = roles.count("testcase_qa")
        return AgentResult(
            role="testcase_qa",
            payload={
                "status": "needs_fix" if qa_round == 1 else "approved",
                "issues": (
                    [
                        {
                            "severity": "major",
                            "category": "correctness",
                            "file": "AI/kserve/agent/tests/test.sh",
                            "description": "test is incomplete",
                            "evidence": "test.sh only runs true",
                            "suggestion": "add a functional assertion",
                        }
                    ]
                    if qa_round == 1
                    else []
                ),
                "coverage_score": 0.5 if qa_round == 1 else 1.0,
                "summary": "repair needed" if qa_round == 1 else "approved",
            },
        )

    result = prepare_upgrade_tests(
        workspace=repo,
        task=_task(),
        base_sha=base_sha,
        checkpoint_dir=tmp_path / "checkpoint",
        report_path=tmp_path / "reports" / "sanitization.json",
        evidence_dir=tmp_path / "reports",
        executable=tmp_path / "opencode",
        api_key="secret",
        agent_runner=agent_runner,
    )

    assert roles == [
        "testcase_creator",
        "testcase_qa",
        "testcase_creator",
        "testcase_qa",
    ]
    assert timeouts == [7200, 2400, 7200, 2400]
    assert result.qa_payload["status"] == "approved"
    assert (tmp_path / "reports/testcase-sanitization-round2.json").is_file()


def test_generated_test_rejects_missing_command_evidence(tmp_path):
    import pytest

    from scripts.lib.oe_upgrade_test_prep import (
        UpgradeTestPreparationError,
        prepare_upgrade_tests,
    )

    repo, base_sha = _candidate(tmp_path, test=False)

    def agent_runner(**kwargs):
        test = repo / "AI/kserve/agent/tests/test.sh"
        test.parent.mkdir(exist_ok=True)
        test.write_text("#!/bin/bash\nset -euo pipefail\ntrue\n")
        test.chmod(0o755)
        return AgentResult(
            role=kwargs["role"],
            payload={
                "success": True,
                "files_created": ["AI/kserve/agent/tests/test.sh"],
                "command_evidence": [],
            },
        )

    with pytest.raises(UpgradeTestPreparationError, match="command_evidence"):
        prepare_upgrade_tests(
            workspace=repo,
            task=_task(),
            base_sha=base_sha,
            checkpoint_dir=tmp_path / "checkpoint",
            report_path=tmp_path / "reports/sanitization.json",
            evidence_dir=tmp_path / "reports",
            executable=tmp_path / "opencode",
            api_key="secret",
            agent_runner=agent_runner,
        )
