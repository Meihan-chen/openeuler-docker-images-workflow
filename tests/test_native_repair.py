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


def _passing_gate():
    return {
        "status": "passed",
        "build_allowed": True,
        "test_allowed": True,
        "delivery_allowed": True,
        "findings": [],
    }


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
            "checks": {
                "native_build": True,
                "dgoss": True,
                "shared_tests": True,
            },
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
        target_validator=lambda **_: _passing_gate(),
    )

    summary_path = evidence / "agents" / "native-repair-x86_64.json"
    assert result.repair_attempts == 0
    assert summary_path.is_file()
    assert json.loads(summary_path.read_text()) == {
        "architecture": "x86_64",
        "repair_attempts": 0,
        "status": "passed",
    }


def test_native_gate_requires_explicit_build_and_test_permissions(tmp_path):
    from scripts.lib.native_repair import NativeRepairError, decide_round

    workspace = tmp_path / "target"
    workspace.mkdir()
    fixer = Fixer()

    with pytest.raises(NativeRepairError, match="target contract"):
        decide_round(
            workspace=workspace,
            task=_task(),
            base_sha="1" * 40,
            round_number=1,
            max_rounds=3,
            reports={
                "x86_64": _report("x86_64", status="failed"),
                "aarch64": _report("aarch64"),
            },
            report_dir=tmp_path / "reports",
            executable=tmp_path / "opencode",
            api_key="deepseek-secret",
            agent_runner=fixer,
            target_validator=lambda **_: {"status": "passed"},
        )

    assert len(fixer.calls) == 1


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
        return _passing_gate()

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


def test_native_fixer_can_edit_existing_auxiliary_candidate_files(tmp_path):
    from scripts.lib.native_repair import validate_native_with_repairs

    workspace = tmp_path / "target"
    evidence = tmp_path / "evidence"
    image_root = (
        workspace
        / "Database"
        / "kvrocks"
        / "2.16.0"
        / "24.03-lts-sp4"
    )
    image_root.mkdir(parents=True)
    (image_root / "service.conf").write_text("listen = 0.0.0.0\n")
    fixer = Fixer()

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
        native_validator=NativeValidator(failures=1),
        agent_runner=fixer,
        target_validator=lambda **_: _passing_gate(),
    )

    assert result.status == "passed"
    assert (
        "Database/kvrocks/2.16.0/24.03-lts-sp4/service.conf"
        in fixer.calls[0]["prompt"]
    )


def test_delivery_only_gate_finding_does_not_block_native_validation(tmp_path):
    from scripts.lib.native_repair import validate_native_with_repairs

    workspace = tmp_path / "target"
    evidence = tmp_path / "evidence"
    workspace.mkdir()
    validator = NativeValidator(failures=0)
    fixer = Fixer()
    gate = {
        "status": "passed",
        "build_allowed": True,
        "test_allowed": True,
        "delivery_allowed": False,
        "errors": ["README metadata is incomplete"],
        "findings": [
            {
                "code": "readme.required",
                "level": "delivery_stop",
                "owner": "image_creator",
                "message": "README metadata is incomplete",
            }
        ],
    }

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
        target_validator=lambda **_: gate,
    )

    assert result.status == "passed"
    assert result.repair_attempts == 0
    assert len(validator.calls) == 1
    assert fixer.calls == []


def test_native_repair_returns_needs_human_after_exactly_three_fixes(tmp_path):
    from scripts.lib.native_repair import validate_native_with_repairs

    workspace = tmp_path / "target"
    workspace.mkdir()
    validator = NativeValidator(failures=4)
    fixer = Fixer()

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
        target_validator=lambda **_: _passing_gate(),
    )

    assert result.status == "needs-human-review"
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
            _passing_gate(),
            {
                "status": "passed",
                "build_allowed": True,
                "test_allowed": False,
                "delivery_allowed": False,
                "errors": ["goss_wait timeout is invalid"],
                "findings": [
                    {
                        "code": "tests.wait_timeout",
                        "level": "delivery_stop",
                        "owner": "testcase_creator",
                        "message": "goss_wait timeout is invalid",
                    }
                ],
            },
            _passing_gate(),
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
    from scripts.lib.native_repair import validate_native_with_repairs

    workspace = tmp_path / "target"
    workspace.mkdir()
    validator = NativeValidator(failures=1)
    fixer = Fixer()

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
        target_validator=lambda **_: {
            "status": "passed",
            "build_allowed": True,
            "test_allowed": False,
            "delivery_allowed": False,
            "errors": ["still invalid"],
            "findings": [
                {
                    "code": "tests.invalid",
                    "level": "delivery_stop",
                    "owner": "testcase_creator",
                    "message": "still invalid",
                }
            ],
        },
    )

    assert result.status == "needs-human-review"
    assert result.repair_attempts == 3
    assert len(validator.calls) == 0
    assert len(fixer.calls) == 3
    terminal = json.loads((tmp_path / "agents/convergence-report.json").read_text())
    assert terminal["reason"] == "target-contract-repair-exhausted"


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
            target_validator=lambda **_: _passing_gate(),
        )

    report = json.loads(
        (repair_dir / "fixer-native-x86_64-round1.json").read_text()
    )
    assert report["status"] == "insufficient_evidence"


DIGEST = "c" * 64


def _report(architecture, *, status="passed", digest=DIGEST):
    report = {
        "status": status,
        "architecture": architecture,
        "checks": {
            "native_build": status == "passed",
            "dgoss": status == "passed",
            "shared_tests": status == "passed",
        },
    }
    if status == "passed":
        report["validated_patch_sha256"] = digest
    else:
        report["failure"] = f"{architecture} build failed"
    return report


def _mixed_infra_and_format_report(architecture):
    report = _report(architecture, status="failed")
    report.update(
        {
            "failed_stage": "native_build",
            "failure": "timed out",
            "failure_details": {"returncode": 124},
            "failures": [
                {
                    "stage": "native_build",
                    "check": "native_build",
                    "failure": "timed out",
                    "failure_details": {"returncode": 124},
                }
            ],
            "format_check": {
                "status": "failed",
                "kind": "candidate",
                "failure": "image-info.yml is missing environment",
            },
        }
    )
    return report


def _decide(tmp_path, reports, **kwargs):
    from scripts.lib.native_repair import decide_round

    defaults = dict(
        workspace=tmp_path / "target",
        task=_task(),
        base_sha="1" * 40,
        round_number=1,
        max_rounds=3,
        reports=reports,
        report_dir=tmp_path / "evidence",
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=Fixer(),
        target_validator=lambda **_: _passing_gate(),
    )
    (tmp_path / "target").mkdir(exist_ok=True)
    return decide_round(**{**defaults, **kwargs})


def test_a_round_that_passes_on_both_architectures_is_itself_the_proof(
    tmp_path,
):
    fixer = Fixer()

    decision = _decide(
        tmp_path,
        {"x86_64": _report("x86_64"), "aarch64": _report("aarch64")},
        agent_runner=fixer,
    )

    assert decision.converged is True
    assert decision.validated_patch_sha256 == DIGEST
    # Convergence is proof on its own; no separate revalidation, no Fixer.
    assert fixer.calls == []


def test_both_passing_on_different_candidates_is_never_convergence(tmp_path):
    from scripts.lib.native_repair import NativeRepairError

    with pytest.raises(NativeRepairError, match="same validated candidate"):
        _decide(
            tmp_path,
            {
                "x86_64": _report("x86_64"),
                "aarch64": _report("aarch64", digest="d" * 64),
            },
        )


def test_different_format_checker_commits_retry_without_calling_fixer(tmp_path):
    fixer = Fixer()
    x86 = _report("x86_64")
    arm = _report("aarch64")
    x86["format_check"] = {
        "status": "passed",
        "kind": "candidate",
        "commit_sha": "a" * 40,
    }
    arm["format_check"] = {
        "status": "passed",
        "kind": "candidate",
        "commit_sha": "b" * 40,
    }

    decision = _decide(
        tmp_path,
        {"x86_64": x86, "aarch64": arm},
        agent_runner=fixer,
    )

    assert decision.converged is False
    assert decision.repair_attempts == 0
    assert fixer.calls == []


def test_format_failure_evidence_is_included_in_the_dual_arch_fixer(tmp_path):
    fixer = Fixer()
    reports = {}
    for architecture in ("x86_64", "aarch64"):
        report = _report(architecture)
        report.update(
            {
                "status": "failed",
                "failed_stage": "upstream_format",
                "failure": "upstream format check reported 1 failure",
                "failure_details": {"kind": "candidate"},
                "format_check": {
                    "status": "failed",
                    "kind": "candidate",
                    "commit_sha": "a" * 40,
                    "output": "image-info.yml is missing environment",
                },
            }
        )
        reports[architecture] = report

    _decide(tmp_path, reports, agent_runner=fixer)

    prompt = fixer.calls[0]["prompt"]
    assert "image-info.yml is missing environment" in prompt
    assert "x86_64" in prompt
    assert "aarch64" in prompt
    fixer_report = json.loads(
        (
            tmp_path
            / "evidence"
            / "fixer-native-dual-round1-attempt1.json"
        ).read_text()
    )
    classification = fixer_report["_input_review"]["classification"]
    assert classification["x86_64"]["category"] == "image-contract"
    assert classification["aarch64"]["category"] == "image-contract"


def test_round_rejects_passed_report_with_incomplete_check_set(tmp_path):
    from scripts.lib.native_repair import NativeRepairError

    partial = _report("x86_64")
    partial["checks"] = {"native_build": True}

    with pytest.raises(NativeRepairError, match="checks are incomplete"):
        _decide(
            tmp_path,
            {
                "x86_64": partial,
                "aarch64": _report("aarch64"),
            },
        )


def test_one_failing_architecture_repairs_once_with_both_reports(tmp_path):
    fixer = Fixer()

    decision = _decide(
        tmp_path,
        {
            "x86_64": _report("x86_64"),
            "aarch64": _report("aarch64", status="failed"),
        },
        agent_runner=fixer,
    )

    assert decision.converged is False
    assert decision.repair_attempts == 1
    review = fixer.calls[0]["prompt"]
    # A fix driven by one architecture alone can silently regress the other.
    assert "x86_64" in review
    assert "aarch64" in review
    assert "deepseek-secret" not in review


def test_dual_architecture_fixer_gets_external_native_evidence_paths(tmp_path):
    fixer = Fixer()
    evidence_roots = {
        "x86_64": tmp_path / "phase1-x86" / "diagnostics",
        "aarch64": tmp_path / "phase1-arm" / "diagnostics",
    }
    for architecture, root in evidence_roots.items():
        root.mkdir(parents=True)
        (root / "runtime.docker.log").write_text(
            f"{architecture} complete runtime log\n"
        )

    _decide(
        tmp_path,
        {
            "x86_64": _report("x86_64", status="failed"),
            "aarch64": _report("aarch64", status="failed"),
        },
        agent_runner=fixer,
        evidence_roots=evidence_roots,
    )

    call = fixer.calls[0]
    assert call["external_read_dirs"] == tuple(
        root.resolve() for root in evidence_roots.values()
    )
    prompt = call["prompt"]
    assert '"full_evidence"' in prompt
    for architecture, root in evidence_roots.items():
        assert architecture in prompt
        assert str(root.resolve()) in prompt
        assert str((root / "runtime.docker.log").resolve()) in prompt


def test_dual_architecture_fixer_cannot_change_read_only_native_evidence(tmp_path):
    from scripts.lib.agent_runtime import AgentResult
    from scripts.lib.native_repair import NativeRepairError

    evidence_root = tmp_path / "phase1-x86" / "diagnostics"
    evidence_root.mkdir(parents=True)
    log_path = evidence_root / "runtime.docker.log"
    log_path.write_text("authoritative runtime log\n")

    def mutating_fixer(**kwargs):
        log_path.write_text("tampered\n")
        return AgentResult(
            role="fixer",
            payload={"success": True, "changes": [], "summary": "fixed"},
        )

    with pytest.raises(NativeRepairError, match="read-only native evidence changed"):
        _decide(
            tmp_path,
            {
                "x86_64": _report("x86_64", status="failed"),
                "aarch64": _report("aarch64"),
            },
            agent_runner=mutating_fixer,
            evidence_roots={"x86_64": evidence_root},
        )


def test_dual_architecture_fixer_gets_one_hour_timeout(tmp_path):
    fixer = Fixer()

    _decide(
        tmp_path,
        {
            "x86_64": _report("x86_64"),
            "aarch64": _report("aarch64", status="failed"),
        },
        agent_runner=fixer,
    )

    assert fixer.calls[0]["timeout"] == 3600


def test_fixer_receives_a_classification_for_every_native_check_failure(
    tmp_path,
):
    fixer = Fixer()
    x86 = _report("x86_64", status="failed")
    x86.update(
        {
            "failed_stage": "dgoss",
            "failure": "invalid Attribute for File:/tmp: dir",
            "failure_details": {"returncode": 1},
            "failures": [
                {
                    "stage": "dgoss",
                    "failure": "invalid Attribute for File:/tmp: dir",
                    "failure_details": {"returncode": 1},
                },
                {
                    "stage": "shared_tests",
                    "failure": "version assertion failed",
                    "failure_details": {"returncode": 1},
                },
            ],
        }
    )

    _decide(
        tmp_path,
        {"x86_64": x86, "aarch64": _report("aarch64")},
        agent_runner=fixer,
    )

    report = json.loads(
        (tmp_path / "evidence" / "fixer-native-dual-round1-attempt1.json").read_text()
    )
    classification = report["_input_review"]["classification"]["x86_64"]
    assert [failure["stage"] for failure in classification["failures"]] == [
        "dgoss",
        "shared_tests",
    ]
    assert [failure["category"] for failure in classification["failures"]] == [
        "config-parse",
        "runtime-error",
    ]


def test_a_repair_breaking_a_hard_boundary_stops_without_reasking_fixer(tmp_path):
    fixer = Fixer()
    hard_stop = {
        "status": "failed",
        "build_allowed": False,
        "errors": ["change outside task scope"],
        "findings": [
            {
                "code": "scope.changed_path",
                "level": "hard_stop",
                "owner": "workflow",
                "message": "change outside task scope",
            }
        ],
    }

    decision = _decide(
        tmp_path,
        {
            "x86_64": _report("x86_64", status="failed"),
            "aarch64": _report("aarch64"),
        },
        agent_runner=fixer,
        target_validator=lambda **_: hard_stop,
    )

    assert len(fixer.calls) == 1
    assert decision.converged is False
    assert decision.terminal_status == "hard-stop"
    report = json.loads(
        (tmp_path / "evidence" / "convergence-report.json").read_text()
    )
    assert report["status"] == "hard-stop"
    assert report["reason"] == "target-contract-hard-stop"
    assert report["gate"]["findings"][0]["code"] == "scope.changed_path"


def test_target_contract_exception_preserves_hard_stop_findings(tmp_path):
    from scripts.lib.target_contract import TargetContractError

    fixer = Fixer()

    def hard_stop(**_):
        raise TargetContractError(
            "meta.yml is invalid",
            findings=[
                {
                    "code": "meta.invalid_yaml",
                    "level": "hard_stop",
                    "owner": "workflow",
                    "message": "meta.yml is invalid",
                }
            ],
        )

    decision = _decide(
        tmp_path,
        {
            "x86_64": _report("x86_64", status="failed"),
            "aarch64": _report("aarch64"),
        },
        agent_runner=fixer,
        target_validator=hard_stop,
    )

    assert len(fixer.calls) == 1
    assert decision.terminal_status == "hard-stop"
    report = json.loads(
        (tmp_path / "evidence" / "convergence-report.json").read_text()
    )
    assert report["gate"]["findings"][0]["code"] == "meta.invalid_yaml"


def test_unsuccessful_fixer_cannot_hide_a_hard_boundary_change(tmp_path):
    hard_stop = {
        "status": "failed",
        "build_allowed": False,
        "test_allowed": False,
        "delivery_allowed": False,
        "errors": ["change outside task scope"],
        "findings": [
            {
                "code": "scope.changed_path",
                "level": "hard_stop",
                "owner": "workflow",
                "message": "change outside task scope",
            }
        ],
    }

    decision = _decide(
        tmp_path,
        {
            "x86_64": _report("x86_64", status="failed"),
            "aarch64": _report("aarch64"),
        },
        agent_runner=UnsuccessfulFixer(),
        target_validator=lambda **_: hard_stop,
    )

    assert decision.terminal_status == "hard-stop"
    report = json.loads(
        (tmp_path / "evidence" / "convergence-report.json").read_text()
    )
    assert report["gate"]["findings"][0]["code"] == "scope.changed_path"
    fixer_report = json.loads(
        (
            tmp_path
            / "evidence"
            / "fixer-native-dual-round1-attempt1.json"
        ).read_text()
    )
    assert fixer_report["success"] is False


def test_a_repair_with_one_executable_check_advances_to_rebuild(
    tmp_path,
):
    fixer = Fixer()
    partial_gate = {
        "status": "passed",
        "build_allowed": True,
        "goss_allowed": False,
        "shared_tests_allowed": True,
        "test_allowed": False,
        "delivery_allowed": False,
        "errors": ["goss.yaml must be valid YAML"],
        "findings": [
            {
                "code": "tests.goss_yaml",
                "level": "delivery_stop",
                "owner": "testcase_creator",
                "check": "dgoss",
                "message": "goss.yaml must be valid YAML",
            }
        ],
    }

    decision = _decide(
        tmp_path,
        {
            "x86_64": _report("x86_64", status="failed"),
            "aarch64": _report("aarch64"),
        },
        agent_runner=fixer,
        target_validator=lambda **_: partial_gate,
    )

    assert decision.repair_attempts == 1
    assert len(fixer.calls) == 1


def test_a_repair_with_no_executable_checks_is_re_asked_before_rebuild(
    tmp_path,
):
    fixer = Fixer()
    gates = iter(
        [
            {
                "status": "passed",
                "build_allowed": True,
                "goss_allowed": False,
                "shared_tests_allowed": False,
                "test_allowed": False,
                "delivery_allowed": False,
                "errors": [
                    "goss.yaml must be valid YAML",
                    "test.sh must be valid Bash",
                ],
                "findings": [
                    {
                        "code": "tests.goss_yaml",
                        "level": "delivery_stop",
                        "owner": "testcase_creator",
                        "check": "dgoss",
                        "message": "goss.yaml must be valid YAML",
                    },
                    {
                        "code": "tests.shell_syntax",
                        "level": "delivery_stop",
                        "owner": "testcase_creator",
                        "check": "shared_tests",
                        "message": "test.sh must be valid Bash",
                    }
                ],
            },
            {
                "status": "passed",
                "build_allowed": True,
                "goss_allowed": True,
                "shared_tests_allowed": True,
                "test_allowed": True,
                "delivery_allowed": True,
                "findings": [],
            },
        ]
    )

    decision = _decide(
        tmp_path,
        {
            "x86_64": _report("x86_64", status="failed"),
            "aarch64": _report("aarch64"),
        },
        agent_runner=fixer,
        target_validator=lambda **_: next(gates),
    )

    assert decision.repair_attempts == 2
    assert "tests.goss_yaml" in fixer.calls[1]["prompt"]


def test_format_and_native_failures_are_both_classified_for_the_fixer(tmp_path):
    fixer = Fixer()
    x86 = _report("x86_64", status="failed")
    x86.update(
        {
            "failed_stage": "dgoss",
            "failure": "invalid Attribute for File:/tmp: dir",
            "failure_details": {"returncode": 1},
            "failures": [
                {
                    "stage": "dgoss",
                    "check": "dgoss",
                    "failure": "invalid Attribute for File:/tmp: dir",
                    "failure_details": {"returncode": 1},
                }
            ],
            "format_check": {
                "status": "failed",
                "kind": "candidate",
                "commit_sha": "a" * 40,
                "failure": "image-info.yml is missing environment",
            },
        }
    )

    _decide(
        tmp_path,
        {"x86_64": x86, "aarch64": _report("aarch64")},
        agent_runner=fixer,
    )

    report = json.loads(
        (tmp_path / "evidence" / "fixer-native-dual-round1-attempt1.json").read_text()
    )
    failures = report["_input_review"]["classification"]["x86_64"]["failures"]
    assert [failure["stage"] for failure in failures] == [
        "dgoss",
        "upstream_format",
    ]
    assert [failure["category"] for failure in failures] == [
        "config-parse",
        "image-contract",
    ]


def test_exhausting_the_repair_budget_returns_auditable_needs_human_state(tmp_path):
    decision = _decide(
        tmp_path,
        {
            "x86_64": _report("x86_64", status="failed"),
            "aarch64": _report("aarch64"),
        },
        round_number=4,
        max_rounds=3,
    )

    assert decision.converged is False
    assert decision.terminal_status == "needs-human-review"
    report = json.loads(
        (tmp_path / "evidence" / "convergence-report.json").read_text()
    )
    assert report["status"] == "needs-human-review"
    assert report["repair_attempts"] == 3
    assert report["architectures"]["x86_64"]["status"] == "failed"
    assert report["architectures"]["aarch64"]["status"] == "passed"


def test_redaction_leaves_evidence_readable_without_a_secret():
    from scripts.lib.native_repair import _redact

    report = {"status": "failed", "failure": "build failed"}

    assert _redact(report, "") == report


def test_zero_repair_budget_fails_smoke_instead_of_returning_needs_human(
    tmp_path,
):
    from scripts.lib.native_repair import NativeRepairError

    with pytest.raises(NativeRepairError, match="zero-repair validation"):
        _decide(
            tmp_path,
            {
                "x86_64": _report("x86_64", status="failed"),
                "aarch64": _report("aarch64"),
            },
            round_number=1,
            max_rounds=0,
            api_key="",
        )


def test_exhausted_infrastructure_failures_are_not_needs_human(tmp_path):
    from scripts.lib.native_repair import NativeRepairError

    failed = _report("x86_64", status="failed")
    failed.update(
        {
            "failed_stage": "native_build",
            "failure": "timed out",
            "failure_details": {"returncode": 124},
        }
    )

    with pytest.raises(NativeRepairError, match="infrastructure"):
        _decide(
            tmp_path,
            {"x86_64": failed, "aarch64": _report("aarch64")},
            round_number=4,
            max_rounds=3,
        )


def test_exhausted_mixed_infra_and_candidate_failures_need_human_review(tmp_path):
    decision = _decide(
        tmp_path,
        {
            architecture: _mixed_infra_and_format_report(architecture)
            for architecture in ("x86_64", "aarch64")
        },
        round_number=4,
        max_rounds=3,
    )

    assert decision.terminal_status == "needs-human-review"


def test_round_gate_repair_exhaustion_returns_needs_human(tmp_path):
    fixer = Fixer()
    gate = {
        "status": "passed",
        "build_allowed": True,
        "test_allowed": False,
        "delivery_allowed": False,
        "findings": [
            {
                "code": "tests.invalid",
                "level": "delivery_stop",
                "owner": "testcase_creator",
                "message": "tests remain invalid",
            }
        ],
    }

    decision = _decide(
        tmp_path,
        {
            "x86_64": _report("x86_64", status="failed"),
            "aarch64": _report("aarch64"),
        },
        agent_runner=fixer,
        target_validator=lambda **_: gate,
    )

    assert len(fixer.calls) == 3
    assert decision.terminal_status == "needs-human-review"
    terminal = json.loads(
        (tmp_path / "evidence" / "convergence-report.json").read_text()
    )
    assert terminal["reason"] == "target-contract-repair-exhausted"


def test_a_round_decision_needs_both_architectures(tmp_path):
    from scripts.lib.native_repair import NativeRepairError

    with pytest.raises(NativeRepairError, match="aarch64"):
        _decide(tmp_path, {"x86_64": _report("x86_64")})


def test_workspace_hygiene_hard_stops_before_calling_fixer(tmp_path):
    """Run 30567356119 handed the Fixer 496 scope errors for one stray tarball.

    The obvious repair for "change outside task scope" is to revert candidate
    files, which is the opposite of the correct action, so the category and its
    guidance have to travel with the evidence.
    """
    from scripts.lib.native_repair import validate_native_with_repairs

    workspace = tmp_path / "target"
    evidence = tmp_path / "evidence"
    workspace.mkdir()
    validator = NativeValidator(failures=0)
    fixer = Fixer()
    gate = {
        "status": "failed",
        "build_allowed": False,
        "errors": [
            "change outside task scope or wrong status: A "
            "kvrocks-2.16.0/CMakeLists.txt",
            "change outside task scope or wrong status: A "
            "kvrocks-2.16.0/src/cli/main.cc",
        ],
    }

    from scripts.lib.native_repair import NativeRepairError
    with pytest.raises(NativeRepairError, match="hard stop"):
        validate_native_with_repairs(
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
            target_validator=lambda **_: gate,
        )

    assert fixer.calls == []
    assert validator.calls == []


def test_each_architecture_is_classified_before_the_fixer_is_asked(tmp_path):
    """One Fixer sees both architectures, so one category cannot speak for both.

    A Goss config fault on x86 and a build failure on ARM would otherwise be
    handed over as "fix the test configuration and do not change the
    Dockerfile", which is wrong for ARM.
    """
    from scripts.lib.native_repair import decide_round

    workspace = tmp_path / "target"
    workspace.mkdir()
    fixer = Fixer()
    reports = {
        "x86_64": {
            "status": "failed",
            "failed_stage": "dgoss",
            "failure": "invalid Attribute for File:/var/lib/kvrocks: dir",
            "failure_details": {"returncode": 1},
        },
        "aarch64": {
            "status": "failed",
            "failed_stage": "native_build",
            "failure": "libatomic.so.1: cannot open shared object file",
            "failure_details": {"returncode": 1},
        },
    }

    decide_round(
        workspace=workspace,
        task=_task(),
        base_sha="1" * 40,
        round_number=1,
        max_rounds=3,
        reports=reports,
        report_dir=tmp_path / "evidence",
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=fixer,
        target_validator=lambda **_: _passing_gate(),
    )

    prompt = fixer.calls[0]["prompt"]
    assert "config-parse" in prompt
    assert "build-error" in prompt
    report = json.loads(
        (tmp_path / "evidence" / "fixer-native-dual-round1-attempt1.json").read_text()
    )
    per_arch = report["_input_review"]["classification"]
    assert per_arch["x86_64"]["category"] == "config-parse"
    assert per_arch["aarch64"]["category"] == "build-error"


def test_an_infrastructure_failure_does_not_pay_for_a_fixer_call(tmp_path):
    """The Fixer is told not to change anything, so asking it buys nothing.

    The round is still consumed: re-validating the same candidate stays bounded
    by max_rounds, whereas skipping the budget needs a stop condition this
    harness cannot express yet.
    """
    from scripts.lib.native_repair import decide_round

    workspace = tmp_path / "target"
    workspace.mkdir()
    fixer = Fixer()
    timed_out = {
        "status": "failed",
        "failed_stage": "native_build",
        "failure": "timed out",
        "failure_details": {"returncode": 124},
    }

    decision = decide_round(
        workspace=workspace,
        task=_task(),
        base_sha="1" * 40,
        round_number=1,
        max_rounds=3,
        reports={"x86_64": timed_out, "aarch64": timed_out},
        report_dir=tmp_path / "evidence",
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=fixer,
        target_validator=lambda **_: _passing_gate(),
    )

    assert fixer.calls == []
    assert decision.converged is False
    assert decision.repair_attempts == 0


def test_mixed_infra_and_candidate_failures_do_pay_for_a_fixer_call(tmp_path):
    fixer = Fixer()

    decision = _decide(
        tmp_path,
        {
            architecture: _mixed_infra_and_format_report(architecture)
            for architecture in ("x86_64", "aarch64")
        },
        agent_runner=fixer,
    )

    assert len(fixer.calls) == 1
    assert decision.repair_attempts == 1
