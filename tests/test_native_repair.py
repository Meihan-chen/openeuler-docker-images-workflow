import json

import pytest


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


class NativeValidator:
    def __init__(self, failures):
        self.failures = failures
        self.calls = []

    def __call__(self, **kwargs):
        from scripts.lib.native_validation import NativeValidationError

        self.calls.append(kwargs)
        attempt = len(self.calls)
        if attempt <= self.failures:
            report = {
                "status": "failed",
                "architecture": kwargs["architecture"],
                "failure": f"compile failed attempt {attempt}",
            }
            kwargs["report_path"].write_text(json.dumps(report))
            kwargs["junit_path"].write_text("<testsuite failures='1'/>")
            raise NativeValidationError(report["failure"])
        report = {
            "status": "passed",
            "architecture": kwargs["architecture"],
            "checks": {"native_build": True},
        }
        kwargs["report_path"].write_text(json.dumps(report))
        kwargs["junit_path"].write_text("<testsuite failures='0'/>")
        return report


class Fixer:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        from scripts.lib.agent_runtime import AgentResult

        self.calls.append(kwargs)
        return AgentResult(
            role="fixer",
            payload={
                "success": True,
                "changes": [f"repair {len(self.calls)}"],
                "summary": "fixed",
            },
        )


class UnsuccessfulFixer:
    def __call__(self, **kwargs):
        from scripts.lib.agent_runtime import AgentResult

        return AgentResult(
            role="fixer",
            payload={
                "success": False,
                "status": "insufficient_evidence",
                "changes": [],
                "summary": "missing root cause",
            },
        )


def test_zero_repair_success_writes_nonempty_agent_artifact_directory(
    tmp_path,
):
    from scripts.lib.native_repair import validate_native_with_repairs

    workspace = tmp_path / "target"
    evidence = tmp_path / "evidence"
    workspace.mkdir()

    result = validate_native_with_repairs(
        workspace=workspace,
        task=_task(),
        base_sha="1" * 40,
        architecture="x86_64",
        run_id="123456",
        dgoss=tmp_path / "dgoss",
        goss=tmp_path / "goss",
        report_path=evidence / "x86_64.json",
        junit_path=evidence / "x86_64.junit.xml",
        repair_report_dir=evidence / "agents",
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        native_validator=NativeValidator(failures=0),
        agent_runner=Fixer(),
        target_validator=lambda **_: {"status": "passed"},
    )

    summary_path = evidence / "agents" / "native-repair-x86_64.json"
    assert result.repair_attempts == 0
    assert summary_path.is_file()
    assert json.loads(summary_path.read_text()) == {
        "architecture": "x86_64",
        "repair_attempts": 0,
        "status": "passed",
    }


def test_native_failure_is_fixed_gated_and_retried_up_to_success(tmp_path):
    from scripts.lib.native_repair import validate_native_with_repairs

    workspace = tmp_path / "target"
    evidence = tmp_path / "evidence"
    workspace.mkdir()
    validator = NativeValidator(failures=2)
    fixer = Fixer()
    gates = []

    def strict_target_validator(*, repo, task, base_sha):
        gates.append(
            {
                "repo": repo,
                "task": task,
                "base_sha": base_sha,
            }
        )
        return {"status": "passed"}

    result = validate_native_with_repairs(
        workspace=workspace,
        task=_task(),
        base_sha="1" * 40,
        architecture="aarch64",
        run_id="123456",
        dgoss=tmp_path / "dgoss",
        goss=tmp_path / "goss",
        report_path=evidence / "aarch64.json",
        junit_path=evidence / "aarch64.junit.xml",
        repair_report_dir=evidence / "agents",
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        native_validator=validator,
        agent_runner=fixer,
        target_validator=strict_target_validator,
    )

    assert result.status == "passed"
    assert result.repair_attempts == 2
    assert len(validator.calls) == 3
    assert len(fixer.calls) == 2
    assert len(gates) == 3
    assert all(call["repo"] == workspace for call in gates)
    assert all(call["role"] == "fixer" for call in fixer.calls)
    assert "aarch64" in fixer.calls[0]["prompt"]
    assert "compile failed attempt 1" in fixer.calls[0]["prompt"]
    assert "documented `success` and `changes` keys" in fixer.calls[0]["prompt"]
    assert "`files_created` keys" not in fixer.calls[0]["prompt"]
    assert sorted(path.name for path in (evidence / "agents").iterdir()) == [
        "fixer-native-aarch64-round1.json",
        "fixer-native-aarch64-round2.json",
        "native-repair-aarch64.json",
    ]
    reports = [
        json.loads(path.read_text())
        for path in (evidence / "agents").iterdir()
    ]
    assert "deepseek-secret" not in json.dumps(reports)


def test_initial_target_gate_is_fixed_before_native_validation(tmp_path):
    from scripts.lib.native_repair import validate_native_with_repairs

    workspace = tmp_path / "target"
    evidence = tmp_path / "evidence"
    workspace.mkdir()
    validator = NativeValidator(failures=0)
    fixer = Fixer()
    gates = iter(
        (
            {"status": "failed", "errors": ["tcp:6666 is missing"]},
            {"status": "passed"},
        )
    )

    result = validate_native_with_repairs(
        workspace=workspace,
        task=_task(),
        base_sha="1" * 40,
        architecture="x86_64",
        run_id="123456",
        dgoss=tmp_path / "dgoss",
        goss=tmp_path / "goss",
        report_path=evidence / "x86_64.json",
        junit_path=evidence / "x86_64.junit.xml",
        repair_report_dir=evidence / "agents",
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        native_validator=validator,
        agent_runner=fixer,
        target_validator=lambda **_: next(gates),
    )

    assert result.status == "passed"
    assert result.repair_attempts == 1
    assert len(validator.calls) == 1
    assert len(fixer.calls) == 1
    assert "tcp:6666 is missing" in fixer.calls[0]["prompt"]


def test_native_repair_fails_closed_after_exactly_three_fixes(tmp_path):
    from scripts.lib.native_repair import (
        NativeRepairError,
        validate_native_with_repairs,
    )

    workspace = tmp_path / "target"
    workspace.mkdir()
    validator = NativeValidator(failures=4)
    fixer = Fixer()

    with pytest.raises(NativeRepairError, match="after 3 repair attempts"):
        validate_native_with_repairs(
            workspace=workspace,
            task=_task(),
            base_sha="1" * 40,
            architecture="x86_64",
            run_id="123456",
            dgoss=tmp_path / "dgoss",
            goss=tmp_path / "goss",
            report_path=tmp_path / "x86_64.json",
            junit_path=tmp_path / "x86_64.junit.xml",
            repair_report_dir=tmp_path / "agents",
            executable=tmp_path / "opencode",
            api_key="deepseek-secret",
            native_validator=validator,
            agent_runner=fixer,
            target_validator=lambda **_: {"status": "passed"},
        )

    assert len(validator.calls) == 4
    assert len(fixer.calls) == 3


def test_post_fixer_target_gate_is_returned_before_native_retry(
    tmp_path,
):
    from scripts.lib.native_repair import (
        validate_native_with_repairs,
    )

    workspace = tmp_path / "target"
    workspace.mkdir()
    validator = NativeValidator(failures=1)
    fixer = Fixer()
    gates = iter(
        (
            {"status": "passed"},
            {"status": "failed", "errors": ["goss_wait timeout is invalid"]},
            {"status": "passed"},
        )
    )

    result = validate_native_with_repairs(
        workspace=workspace,
        task=_task(),
        base_sha="1" * 40,
        architecture="x86_64",
        run_id="123456",
        dgoss=tmp_path / "dgoss",
        goss=tmp_path / "goss",
        report_path=tmp_path / "x86_64.json",
        junit_path=tmp_path / "x86_64.junit.xml",
        repair_report_dir=tmp_path / "agents",
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        native_validator=validator,
        agent_runner=fixer,
        target_validator=lambda **_: next(gates),
    )

    assert result.status == "passed"
    assert result.repair_attempts == 2
    assert len(validator.calls) == 2
    assert len(fixer.calls) == 2
    assert "compile failed attempt 1" in fixer.calls[1]["prompt"]
    assert "goss_wait timeout is invalid" in fixer.calls[1]["prompt"]
    second_report = json.loads(
        (
            tmp_path
            / "agents"
            / "fixer-native-x86_64-round2.json"
        ).read_text()
    )
    assert second_report["_input_review"]["gate"]["errors"] == [
        "goss_wait timeout is invalid"
    ]
    assert (
        second_report["_input_review"]["native_failure"]["failure"]
        == "compile failed attempt 1"
    )


def test_target_gate_failure_remains_bounded_by_total_fixer_budget(tmp_path):
    from scripts.lib.native_repair import (
        NativeRepairError,
        validate_native_with_repairs,
    )

    workspace = tmp_path / "target"
    workspace.mkdir()
    validator = NativeValidator(failures=1)
    fixer = Fixer()

    with pytest.raises(
        NativeRepairError,
        match="target contract.*3 repair attempts",
    ):
        validate_native_with_repairs(
            workspace=workspace,
            task=_task(),
            base_sha="1" * 40,
            architecture="x86_64",
            run_id="123456",
            dgoss=tmp_path / "dgoss",
            goss=tmp_path / "goss",
            report_path=tmp_path / "x86_64.json",
            junit_path=tmp_path / "x86_64.junit.xml",
            repair_report_dir=tmp_path / "agents",
            executable=tmp_path / "opencode",
            api_key="deepseek-secret",
            native_validator=validator,
            agent_runner=fixer,
            target_validator=lambda **_: {
                "status": "failed",
                "errors": ["still invalid"],
            },
        )

    assert len(validator.calls) == 0
    assert len(fixer.calls) == 3


def test_initial_gate_infrastructure_error_does_not_call_fixer(tmp_path):
    from scripts.lib.native_repair import validate_native_with_repairs

    workspace = tmp_path / "target"
    workspace.mkdir()
    validator = NativeValidator(failures=0)
    fixer = Fixer()

    def unavailable_gate(**_):
        raise OSError("git executable is unavailable")

    with pytest.raises(OSError, match="git executable"):
        validate_native_with_repairs(
            workspace=workspace,
            task=_task(),
            base_sha="1" * 40,
            architecture="x86_64",
            run_id="123456",
            dgoss=tmp_path / "dgoss",
            goss=tmp_path / "goss",
            report_path=tmp_path / "x86_64.json",
            junit_path=tmp_path / "x86_64.junit.xml",
            repair_report_dir=tmp_path / "agents",
            executable=tmp_path / "opencode",
            api_key="deepseek-secret",
            native_validator=validator,
            agent_runner=fixer,
            target_validator=unavailable_gate,
        )

    assert validator.calls == []
    assert fixer.calls == []


def test_unsuccessful_fixer_report_is_retained_before_failure(tmp_path):
    from scripts.lib.native_repair import (
        NativeRepairError,
        validate_native_with_repairs,
    )

    workspace = tmp_path / "target"
    workspace.mkdir()
    repair_dir = tmp_path / "agents"

    with pytest.raises(NativeRepairError, match="fixer failed"):
        validate_native_with_repairs(
            workspace=workspace,
            task=_task(),
            base_sha="1" * 40,
            architecture="x86_64",
            run_id="123456",
            dgoss=tmp_path / "dgoss",
            goss=tmp_path / "goss",
            report_path=tmp_path / "x86_64.json",
            junit_path=tmp_path / "x86_64.junit.xml",
            repair_report_dir=repair_dir,
            executable=tmp_path / "opencode",
            api_key="deepseek-secret",
            native_validator=NativeValidator(failures=1),
            agent_runner=UnsuccessfulFixer(),
            target_validator=lambda **_: {"status": "passed"},
        )

    report = json.loads(
        (repair_dir / "fixer-native-x86_64-round1.json").read_text()
    )
    assert report["status"] == "insufficient_evidence"
