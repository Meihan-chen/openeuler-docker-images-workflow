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


class StubAgent:
    def __init__(self, responses):
        self.responses = {
            role: list(payloads) for role, payloads in responses.items()
        }
        self.calls = []

    def __call__(self, **kwargs):
        from scripts.lib.agent_runtime import AgentResult

        self.calls.append(kwargs)
        role = kwargs["role"]
        return AgentResult(role=role, payload=self.responses[role].pop(0))


def _approved_tests():
    return {
        "status": "approved",
        "issues": [],
        "coverage_score": 0.95,
        "summary": "approved",
    }


def _test_issue(description="fix version check", severity="major"):
    return {
        "severity": severity,
        "file": "Database/kvrocks/tests/test.sh",
        "description": description,
        "evidence": "Database/kvrocks/tests/test.sh contains the defective assertion",
    }


def _image_creator_output(**overrides):
    payload = {
        "success": True,
        "files_created": ["Database/kvrocks/meta.yml"],
        "identity_decision": {
            "mode": "dynamic",
            "user": "kvrocks",
            "group": "kvrocks",
            "uid": None,
            "gid": None,
        },
    }
    payload.update(overrides)
    return payload


def _testcase_creator_output(**overrides):
    payload = {
        "success": True,
        "files_created": ["Database/kvrocks/tests/test.sh"],
        "command_evidence": [
            {
                "command": "redis-cli PING",
                "semantics": "returns PONG once the server accepts connections",
                "evidence_id": "command-ping-001",
            }
        ],
        "evidence": [
            {
                "id": "command-ping-001",
                "claim": "PING returns PONG once the server accepts connections",
                "source": (
                    "https://github.com/apache/kvrocks/blob/"
                    "v2.16.0/src/commands/cmd_server.cc"
                ),
                "excerpts": ["Ping::Execute"],
            }
        ],
    }
    payload.update(overrides)
    return payload


def _fully_approved_agent(*, testcase_summary="approved"):
    return StubAgent(
        {
            "image_creator": [_image_creator_output()],
            "testcase_creator": [_testcase_creator_output()],
            "testcase_qa": [
                {**_approved_tests(), "summary": testcase_summary}
            ],
        }
    )


def _repairable_gate(*, owner, code, message):
    return {
        "status": "passed",
        "build_allowed": True,
        "delivery_allowed": False,
        "test_allowed": owner != "testcase_creator",
        "errors": [message],
        "findings": [
            {
                "code": code,
                "level": "delivery_stop",
                "owner": owner,
                "message": message,
            }
        ],
    }


def _agent_with_testcase_repair():
    image_creator = _image_creator_output()
    testcase_creator = _testcase_creator_output()
    testcase_qa = [
        {
            "status": "needs_fix",
            "issues": [_test_issue()],
            "coverage_score": 0.70,
            "summary": "one issue",
        },
        _approved_tests(),
    ]
    return StubAgent(
        {
            "image_creator": [image_creator],
            "testcase_creator": [testcase_creator] * 2,
            "testcase_qa": testcase_qa,
        }
    )


def _run_recorded_pipeline(
    tmp_path,
    agent,
    *,
    lint=False,
    mutation=None,
):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    events = []

    def recording_agent(**kwargs):
        events.append(f"agent:{kwargs['role']}")
        result = agent(**kwargs)
        if mutation is not None:
            mutation(kwargs["role"], workspace)
        return result

    def validator(*, phase, **_):
        events.append(f"gate:{phase}")
        return {"status": "passed", "phase": phase}

    def image_linter(dockerfile):
        events.append(f"lint:{dockerfile.name}")
        return {"status": "passed"}

    run_generation_pipeline(
        workspace=workspace,
        report_dir=reports,
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=recording_agent,
        target_validator=validator,
        image_linter=image_linter if lint else None,
    )
    return events, reports


def test_generation_lints_image_before_testcase_generation(tmp_path):
    events, reports = _run_recorded_pipeline(
        tmp_path,
        _fully_approved_agent(),
        lint=True,
    )

    assert events == [
        "agent:image_creator",
        "gate:image",
        "lint:Dockerfile",
        "agent:testcase_creator",
        "gate:full",
        "agent:testcase_qa",
        "gate:full",
    ]
    assert json.loads((reports / "image-lint.json").read_text()) == {
        "status": "passed"
    }


def test_generation_skips_image_qa_but_keeps_deterministic_image_checks(
    tmp_path,
):
    events, _ = _run_recorded_pipeline(
        tmp_path,
        _fully_approved_agent(),
        lint=True,
    )

    assert events == [
        "agent:image_creator",
        "gate:image",
        "lint:Dockerfile",
        "agent:testcase_creator",
        "gate:full",
        "agent:testcase_qa",
        "gate:full",
    ]


def test_hadolint_runner_reports_findings_without_policy_allowlist(
    tmp_path,
    monkeypatch,
):
    from scripts.lib import generation_pipeline

    executable = tmp_path / "hadolint"
    dockerfile = tmp_path / "Dockerfile"
    commands = []

    def failed_hadolint(command, **_):
        commands.append(command)
        return type(
            "Result",
            (),
            {
                "returncode": 1,
                "stdout": "",
                "stderr": "DL3006 pin image tag\n",
            },
        )()

    monkeypatch.setattr(
        generation_pipeline.subprocess,
        "run",
        failed_hadolint,
    )
    report = generation_pipeline.lint_dockerfile(
        executable=executable,
        dockerfile=dockerfile,
    )

    assert report["status"] == "passed"
    assert report["diagnostic_status"] == "findings"
    assert report["blocking"] is False
    assert report["output"] == "DL3006 pin image tag"
    assert commands == [[
        str(executable),
        str(dockerfile),
    ]]

    def unavailable_hadolint(command, **_):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(
        generation_pipeline.subprocess,
        "run",
        unavailable_hadolint,
    )
    unavailable = generation_pipeline.lint_dockerfile(
        executable=tmp_path / "missing-hadolint",
        dockerfile=dockerfile,
    )
    assert unavailable["status"] == "failed"
    assert unavailable["blocking"] is True
    assert unavailable["returncode"] is None
    assert "missing-hadolint" in unavailable["output"]


def test_generation_records_hadolint_findings_without_creator_repair(
    tmp_path,
    capsys,
):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    image_creator = _image_creator_output()
    agent = StubAgent(
        {
            "image_creator": [image_creator],
            "testcase_creator": [_testcase_creator_output()],
            "testcase_qa": [_approved_tests()],
        }
    )
    lint_report = {
        "status": "passed",
        "diagnostic_status": "findings",
        "blocking": False,
        "returncode": 1,
        "output": "Dockerfile:9 DL3033 pin yum packages",
    }

    result = run_generation_pipeline(
        workspace=workspace,
        report_dir=reports,
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=lambda **_: {"status": "passed"},
        image_linter=lambda _: lint_report,
    )

    assert result.status == "passed"
    assert [call["role"] for call in agent.calls] == [
        "image_creator",
        "testcase_creator",
        "testcase_qa",
    ]
    assert all(
        "DL3033 pin yum packages" not in call["prompt"]
        for call in agent.calls
    )
    assert json.loads((reports / "image-lint.json").read_text()) == lint_report
    assert not (reports / "image-precheck-repair-lint.json").exists()
    assert (
        "[flow][lint] ADVISORY image_lint: "
        "Dockerfile:9 DL3033 pin yum packages"
    ) in capsys.readouterr().out


def test_generation_returns_failed_testcase_gate_to_creator_once(tmp_path):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    testcase_creator = {
        "success": True,
        "files_created": ["Database/kvrocks/tests/test.sh"],
    }
    agent = StubAgent(
        {
            "image_creator": [_image_creator_output()],
            "testcase_creator": [testcase_creator, testcase_creator],
            "testcase_qa": [_approved_tests()],
        }
    )
    full_results = iter(
        (
            _repairable_gate(
                owner="testcase_creator",
                code="tests.required",
                message="required generated file is missing: tests/test.sh",
            ),
            {
                "status": "passed",
                "build_allowed": True,
                "delivery_allowed": True,
                "test_allowed": True,
                "findings": [],
            },
            {
                "status": "passed",
                "build_allowed": True,
                "delivery_allowed": True,
                "test_allowed": True,
                "findings": [],
            },
        )
    )

    def validator(*, phase, **_):
        if phase == "image":
            return {"status": "passed"}
        return next(full_results)

    result = run_generation_pipeline(
        workspace=workspace,
        report_dir=reports,
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=validator,
    )

    assert result.status == "passed"
    assert [call["role"] for call in agent.calls] == [
        "image_creator",
        "testcase_creator",
        "testcase_creator",
        "testcase_qa",
    ]
    assert "required generated file is missing" in agent.calls[2]["prompt"]
    assert json.loads((reports / "precheck-gates.json").read_text())[
        "delivery_allowed"
    ] is False
    assert json.loads((reports / "precheck-repair-gates.json").read_text())[
        "status"
    ] == "passed"


def test_generation_continues_when_image_delivery_finding_survives_repair(
    tmp_path,
):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    image_creator = _image_creator_output(
        files_created=["Database/kvrocks/README.md"],
    )
    agent = _fully_approved_agent()
    agent.responses["image_creator"].append(image_creator)
    unresolved = _repairable_gate(
        owner="image_creator",
        code="readme.section",
        message="README.md is missing section: # Usage",
    )

    def validator(*, phase, **_):
        return unresolved if phase == "image" else {
            "status": "passed",
            "build_allowed": True,
            "delivery_allowed": True,
            "test_allowed": True,
            "findings": [],
        }

    result = run_generation_pipeline(
        workspace=workspace,
        report_dir=reports,
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=validator,
    )

    assert result.status == "passed"
    assert [call["role"] for call in agent.calls][:3] == [
        "image_creator",
        "image_creator",
        "testcase_creator",
    ]
    assert json.loads(
        (reports / "image-precheck-repair-gates.json").read_text()
    )["delivery_allowed"] is False


def test_generation_continues_to_qa_when_test_contract_survives_repair(
    tmp_path,
):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    testcase_creator = {
        "success": True,
        "files_created": ["Database/kvrocks/tests/test.sh"],
    }
    agent = _fully_approved_agent()
    agent.responses["testcase_creator"].append(testcase_creator)
    full_calls = 0

    def validator(*, phase, **_):
        nonlocal full_calls
        if phase == "image":
            return {"status": "passed"}
        full_calls += 1
        return _repairable_gate(
            owner="testcase_creator",
            code="tests.shell_syntax",
            message="test.sh must be valid Bash",
        )

    result = run_generation_pipeline(
        workspace=workspace,
        report_dir=reports,
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=validator,
    )

    assert result.status == "passed"
    assert result.gate_report["delivery_allowed"] is False
    assert result.gate_report["test_allowed"] is False
    assert [call["role"] for call in agent.calls][-2:] == [
        "testcase_creator",
        "testcase_qa",
    ]
    assert full_calls == 3


@pytest.mark.parametrize(
    "failure",
    [
        "gate",
        "decode",
    ],
)
def test_generation_hard_stops_before_image_qa_without_agent_repair(
    tmp_path,
    failure,
):
    from scripts.lib.generation_pipeline import (
        GenerationPipelineError,
        run_generation_pipeline,
    )

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    agent = _fully_approved_agent()

    def validator(**_):
        if failure == "decode":
            raise UnicodeDecodeError(
                "utf-8",
                b"\xff",
                0,
                1,
                "invalid start byte",
            )
        return {
            "status": "failed",
            "build_allowed": False,
            "delivery_allowed": False,
            "findings": [
                {
                    "code": "scope.outside_task",
                    "level": "hard_stop",
                    "owner": "workflow",
                    "message": "change outside task scope",
                }
            ],
        }

    with pytest.raises(
        GenerationPipelineError,
        match="deterministic image precheck",
    ):
        run_generation_pipeline(
            workspace=workspace,
            report_dir=reports,
            task=_task(),
            base_sha="1" * 40,
            executable=tmp_path / "opencode",
            api_key="deepseek-secret",
            agent_runner=agent,
            target_validator=validator,
        )

    assert [call["role"] for call in agent.calls] == ["image_creator"]
    assert json.loads((reports / "image-precheck-gates.json").read_text())[
        "status"
    ] == "failed"
    assert not (reports / "image-creator-precheck-repair.json").exists()


@pytest.mark.parametrize(
    ("repair", "report_name"),
    [
        (False, "testcase-ownership.json"),
        (True, "testcase-repair-ownership.json"),
    ],
)
def test_testcase_creator_cannot_change_image_owned_files(
    tmp_path,
    repair,
    report_name,
):
    dockerfile = (
        tmp_path
        / "target"
        / "Database"
        / "kvrocks"
        / "2.16.0"
        / "24.03-lts-sp4"
        / "Dockerfile"
    )
    agent = (
        _agent_with_testcase_repair()
        if repair
        else _fully_approved_agent()
    )
    testcase_calls = 0

    def mutate(role, _):
        nonlocal testcase_calls
        if role == "image_creator":
            dockerfile.parent.mkdir(parents=True)
            dockerfile.write_text(
                "FROM scratch\n"
                "RUN groupadd -r kvrocks && useradd -r -g kvrocks kvrocks\n"
                "USER kvrocks\n"
            )
        elif role == "testcase_creator":
            testcase_calls += 1
            if testcase_calls == (2 if repair else 1):
                dockerfile.write_text(
                    dockerfile.read_text() + "RUN echo changed\n"
                )

    from scripts.lib.generation_pipeline import GenerationPipelineError
    with pytest.raises(
        GenerationPipelineError,
        match="testcase_creator.*changed image-owned content",
    ):
        _run_recorded_pipeline(
            tmp_path,
            agent,
            mutation=mutate,
        )

    roles = [call["role"] for call in agent.calls]
    assert roles[-1] == "testcase_creator"
    assert roles.count("testcase_qa") == (1 if repair else 0)
    ownership = json.loads(
        (tmp_path / "evidence" / report_name).read_text()
    )
    assert ownership["status"] == "failed"
    assert "Database/kvrocks/2.16.0/24.03-lts-sp4/Dockerfile" in ownership[
        "changed_files"
    ]


def test_generation_rechecks_creator_repair_before_second_qa(
    tmp_path,
):
    events, _ = _run_recorded_pipeline(
        tmp_path,
        _agent_with_testcase_repair(),
        lint=True,
    )

    assert events[-7:-1] == [
        "agent:testcase_creator",
        "gate:full",
        "agent:testcase_qa",
        "agent:testcase_creator",
        "gate:full",
        "agent:testcase_qa",
    ]


def test_generation_runs_testcase_review_pair_and_records_evidence(
    tmp_path,
    capsys,
):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    agent = StubAgent(
        {
            "image_creator": [_image_creator_output()],
            "testcase_creator": [
                _testcase_creator_output(),
                _testcase_creator_output(),
            ],
            "testcase_qa": [
                {
                    "status": "needs_fix",
                    "issues": [_test_issue("fix version")],
                    "coverage_score": 0.7,
                    "summary": "one issue",
                },
                _approved_tests(),
            ],
        }
    )
    gate_calls = []

    def validator(**kwargs):
        gate_calls.append(kwargs)
        return {"status": "passed", "added_files": 10}

    result = run_generation_pipeline(
        workspace=workspace,
        report_dir=reports,
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=validator,
    )

    assert result.status == "passed"
    assert result.qa_fix_rounds == 1
    assert [call["role"] for call in agent.calls] == [
        "image_creator",
        "testcase_creator",
        "testcase_qa",
        "testcase_creator",
        "testcase_qa",
    ]
    assert [call["timeout"] for call in agent.calls] == [
        3600,
        3600,
        1200,
        3600,
        1200,
    ]
    assert "Review report to resolve" in agent.calls[3]["prompt"]
    assert "fix version" in agent.calls[3]["prompt"]
    assert "Only fix the reported issues" in agent.calls[3]["prompt"]
    assert (
        "Your final response MUST be exactly one JSON object"
        in agent.calls[3]["prompt"]
    )
    assert "Previous QA findings to verify" not in agent.calls[2]["prompt"]
    assert "Previous QA findings to verify" in agent.calls[4]["prompt"]
    assert "fix version" in agent.calls[4]["prompt"]
    assert "independent QA session" in agent.calls[4]["prompt"]
    assert "complete review" in agent.calls[4]["prompt"]
    assert [call["phase"] for call in gate_calls] == [
        "image",
        "full",
        "full",
        "full",
    ]
    assert gate_calls[0]["workspace"] == workspace
    assert sorted(path.name for path in reports.iterdir()) == [
        "gates.json",
        "image-creator.json",
        "image-precheck-gates.json",
        "precheck-gates.json",
        "testcase-creator-round2.json",
        "testcase-creator.json",
        "testcase-ownership.json",
        "testcase-qa-round1.json",
        "testcase-qa-round2.json",
        "testcase-repair-gates.json",
        "testcase-repair-ownership.json",
        "testcase-round1-evidence-bundle.json",
        "testcase-round2-evidence-bundle.json",
    ]
    gate_report = json.loads((reports / "gates.json").read_text())
    assert gate_report["status"] == "passed"
    assert "deepseek-secret" not in json.dumps(
        [json.loads(path.read_text()) for path in reports.iterdir()]
    )
    output = capsys.readouterr().out
    assert (
        '[flow][review] RESULT testcase_qa round=1 status=needs_fix '
        'issues=1 summary="one issue"'
    ) in output
    assert (
        '[flow][review] RESULT testcase_qa round=2 status=approved '
        'issues=0 summary="approved"'
    ) in output
    markers = [
        "[flow][generate] START image_creator",
        "[flow][generate] PASS image_creator",
        "[flow][generate] START testcase_creator",
        "[flow][gate] PASS generated_precheck",
        "[flow][review] START testcase_qa round=1",
        "[flow][repair] START testcase_creator round=2",
        "[flow][review] PASS testcase_qa round=2",
        "[flow][gate] PASS target_contract",
    ]
    positions = [output.index(marker) for marker in markers]
    assert positions == sorted(positions)


def test_generation_continues_when_second_qa_records_disagreement(
    tmp_path,
    capsys,
):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    needs_fix = {
        "status": "needs_fix",
        "issues": [_test_issue("still broken", severity="blocker")],
        "summary": "not approved",
    }
    agent = StubAgent(
        {
            "image_creator": [_image_creator_output()],
            "testcase_creator": [
                _testcase_creator_output(),
                _testcase_creator_output(),
            ],
            "testcase_qa": [
                {**needs_fix, "coverage_score": 0.5},
                {**needs_fix, "coverage_score": 0.5},
            ],
        }
    )
    gate_calls = []

    def validator(**kwargs):
        gate_calls.append(kwargs)
        return {"status": "passed", "added_files": 10}

    result = run_generation_pipeline(
        workspace=workspace,
        report_dir=reports,
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=validator,
    )

    assert result.status == "passed"
    assert result.qa_disagreements == (
        {
            "role": "testcase_qa",
            "round": 2,
            "issues": needs_fix["issues"],
            "summary": "not approved",
        },
    )
    assert json.loads((reports / "qa-disagreements.json").read_text()) == {
        "status": "passed_with_qa_disagreement",
        "disagreements": list(result.qa_disagreements),
    }
    assert [call["phase"] for call in gate_calls] == [
        "image",
        "full",
        "full",
        "full",
    ]
    assert json.loads((reports / "testcase-qa-round2.json").read_text())[
        "status"
    ] == "needs_fix"
    assert (
        "[flow][review] DISAGREEMENT testcase_qa round=2; "
        "continue=local_validation"
    ) in capsys.readouterr().out


def test_invalid_first_qa_status_without_issues_continues_without_repair(
    tmp_path,
    capsys,
):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    agent = StubAgent(
        {
            "image_creator": [_image_creator_output()],
            "testcase_creator": [_testcase_creator_output()],
            "testcase_qa": [
                {
                    "status": "looks_good",
                    "issues": [],
                    "coverage_score": 0.95,
                    "summary": "No candidate issue was described.",
                }
            ],
        }
    )

    result = run_generation_pipeline(
        workspace=workspace,
        report_dir=reports,
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=lambda **_: {"status": "passed"},
    )

    assert result.status == "passed"
    assert [call["role"] for call in agent.calls] == [
        "image_creator",
        "testcase_creator",
        "testcase_qa",
    ]
    report = json.loads((reports / "testcase-qa-round1.json").read_text())
    assert report["status"] == "approved"
    warning = report["harness"]["protocol_warnings"][0]
    assert warning["reported"] == "looks_good"
    assert warning["field"] == "status"
    assert report["issues"] == []
    assert result.qa_fix_rounds == 0
    assert (
        "[flow][review] WARNING testcase_qa round=1 field=status "
        'reported="looks_good" effective=approved'
    ) in capsys.readouterr().out


@pytest.mark.parametrize("reported_status", (None, 7, [], {}))
def test_invalid_qa_status_types_are_mapped_without_crashing(reported_status):
    from scripts.lib.generation_pipeline import _normalize_qa_payload

    normalized = _normalize_qa_payload(
        {
            "status": reported_status,
            "issues": [],
            "summary": "No candidate issue was described.",
        },
        require_coverage=False,
        snapshot={"status": "full", "complete_text": True},
    )

    assert normalized["status"] == "approved"
    assert normalized["issues"] == []
    assert normalized["harness"]["reported_status"] == reported_status
    assert normalized["harness"]["protocol_warnings"][0]["field"] == "status"


def test_agent_cannot_forge_harness_qa_disposition():
    from scripts.lib.generation_pipeline import _normalize_qa_payload

    normalized = _normalize_qa_payload(
        {
            "status": "needs_fix",
            "issues": [],
            "summary": "No candidate issue was described.",
            "reported_status": "forged",
            "protocol_warnings": [{"field": "status", "message": "forged"}],
            "harness": {
                "reported_status": "forged",
                "protocol_warnings": [
                    {"field": "status", "message": "forged"}
                ],
            },
        },
        require_coverage=False,
        snapshot={"status": "full", "complete_text": True},
    )

    assert normalized["status"] == "approved"
    assert normalized["harness"]["reported_status"] == "needs_fix"
    assert normalized["harness"]["protocol_warnings"][0]["field"] == "status"
    assert "reported_status" not in normalized
    assert "protocol_warnings" not in normalized


def test_invalid_first_qa_status_with_real_issues_uses_creator_repair(
    tmp_path,
):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    agent = StubAgent(
        {
            "image_creator": [_image_creator_output()],
            "testcase_creator": [
                _testcase_creator_output(),
                _testcase_creator_output(),
            ],
            "testcase_qa": [
                {
                    "status": "looks_good",
                    "issues": [_test_issue("The version assertion is too weak.")],
                    "coverage_score": 0.6,
                    "summary": "The generated service is unreachable.",
                },
                _approved_tests(),
            ],
        }
    )

    result = run_generation_pipeline(
        workspace=workspace,
        report_dir=reports,
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=lambda **_: {"status": "passed"},
    )

    assert result.qa_fix_rounds == 1
    assert [call["role"] for call in agent.calls] == [
        "image_creator",
        "testcase_creator",
        "testcase_qa",
        "testcase_creator",
        "testcase_qa",
    ]
    assert "The version assertion is too weak." in agent.calls[3]["prompt"]
    assert "qa_protocol" not in agent.calls[3]["prompt"]


@pytest.mark.parametrize(
    "issue_file",
    [
        "Database/kvrocks/2.16.0/24.03-lts-sp4/Dockerfile",
        "/tmp/tests/test.sh",
        "Other/app/tests/test.sh",
    ],
)
def test_out_of_scope_qa_issue_cannot_trigger_testcase_creator_repair(
    tmp_path,
    issue_file,
):
    agent = _fully_approved_agent()
    agent.responses["testcase_creator"].append(_testcase_creator_output())
    agent.responses["testcase_qa"] = [
        {
            "status": "approved",
            "issues": [
                {
                    "severity": "major",
                    "file": issue_file,
                    "description": "The service binds only to localhost.",
                    "evidence": "Dockerfile binds the service to localhost",
                }
            ],
            "coverage_score": 0.6,
            "summary": "The generated service is unreachable.",
        },
    ]

    _, reports = _run_recorded_pipeline(tmp_path, agent)

    assert [call["role"] for call in agent.calls].count("testcase_creator") == 1
    report = json.loads((reports / "testcase-qa-round1.json").read_text())
    assert report["status"] == "approved"
    assert report["issues"] == []
    assert any(
        warning["field"] == "issues"
        for warning in report["harness"]["protocol_warnings"]
    )


def test_malformed_qa_issues_do_not_trigger_creator_repair():
    from scripts.lib.generation_pipeline import _normalize_qa_payload

    normalized = _normalize_qa_payload(
        {
            "status": "needs_fix",
            "issues": [{}, "not an issue", {"description": "  "}],
            "summary": "No actionable candidate issue was described.",
        },
        require_coverage=False,
        snapshot={"status": "full", "complete_text": True},
    )

    assert normalized["status"] == "approved"
    assert normalized["issues"] == []
    assert any(
        warning["field"] == "issues"
        for warning in normalized["harness"]["protocol_warnings"]
    )


def test_invalid_coverage_score_becomes_warning_without_agent_repair(
    tmp_path,
    capsys,
):
    agent = _fully_approved_agent()
    agent.responses["testcase_qa"] = [
        {
            "status": "approved",
            "issues": [],
            "coverage_score": "high",
            "summary": "Tests are approved.",
        }
    ]

    _, reports = _run_recorded_pipeline(tmp_path, agent)

    assert [call["role"] for call in agent.calls] == [
        "image_creator",
        "testcase_creator",
        "testcase_qa",
    ]
    assert agent.calls[-1]["required_keys"] == ("issues", "summary")
    report = json.loads((reports / "testcase-qa-round1.json").read_text())
    assert report["status"] == "approved"
    assert report["coverage_score"] is None
    assert report["harness"]["reported_coverage_score"] == "high"
    assert report["harness"]["protocol_warnings"][0]["field"] == "coverage_score"
    assert (
        "[flow][review] WARNING testcase_qa round=1 "
        'field=coverage_score reported="high" effective=null'
    ) in capsys.readouterr().out


def test_missing_coverage_score_becomes_unavailable_warning(tmp_path):
    agent = _fully_approved_agent()
    agent.responses["testcase_qa"] = [
        {
            "status": "approved",
            "issues": [],
            "summary": "Tests are approved.",
        }
    ]

    _, reports = _run_recorded_pipeline(tmp_path, agent)

    report = json.loads((reports / "testcase-qa-round1.json").read_text())
    assert report["coverage_score"] is None
    warning = report["harness"]["protocol_warnings"][0]
    assert warning["field"] == "coverage_score"
    assert warning["reported"] is None


def test_image_precheck_repair_flows_directly_to_testcase_generation(
    tmp_path,
):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    initial = {
        "success": True,
        "files_created": ["Database/kvrocks/meta.yml"],
        "identity_decision": {
            "mode": "dynamic",
            "user": "kvrocks",
            "group": "kvrocks",
            "uid": None,
            "gid": None,
        },
    }
    repaired = {
        "success": True,
        "files_created": ["Database/kvrocks/meta.yml"],
        "summary": "deterministic repair applied",
        "identity_decision": {
            "mode": "reuse_existing",
            "user": "root",
            "group": "root",
            "uid": None,
            "gid": None,
        },
    }
    agent = StubAgent(
        {
            "image_creator": [initial, repaired],
            "testcase_creator": [_testcase_creator_output()],
            "testcase_qa": [_approved_tests()],
        }
    )
    image_results = iter(
        (
            _repairable_gate(
                owner="image_creator",
                code="dockerfile.identity",
                message="identity decision needs repair",
            ),
            {"status": "passed"},
        )
    )

    def validator(*, phase, **_):
        if phase == "image":
            return next(image_results)
        return {"status": "passed"}

    run_generation_pipeline(
        workspace=workspace,
        report_dir=reports,
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=validator,
    )

    assert [call["role"] for call in agent.calls] == [
        "image_creator",
        "image_creator",
        "testcase_creator",
        "testcase_qa",
    ]


def test_image_creator_contract_error_joins_the_existing_precheck_repair(
    tmp_path,
):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    workspace.mkdir()
    initial = {
        "success": True,
        "files_created": ["Bigdata/kylin-e2e-test/meta.yml"],
        "identity_decision": {
            "mode": "dynamic",
            "user": None,
            "group": None,
            "uid": None,
            "gid": None,
        },
    }
    repaired = {
        **initial,
        "identity_decision": {
            "mode": "reuse_existing",
            "user": "root",
            "group": "root",
            "uid": None,
            "gid": None,
        },
    }
    agent = StubAgent(
        {
            "image_creator": [initial, repaired],
            "testcase_creator": [_testcase_creator_output()],
            "testcase_qa": [_approved_tests()],
        }
    )

    run_generation_pipeline(
        workspace=workspace,
        report_dir=tmp_path / "evidence",
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=lambda **_: {"status": "passed"},
    )

    assert [call["role"] for call in agent.calls].count("image_creator") == 2
    report = json.loads(
        (tmp_path / "evidence" / "image-precheck-gates.json").read_text()
    )
    assert report["delivery_allowed"] is False
    assert report["findings"][0]["owner"] == "image_creator"
    assert report["findings"][0]["code"] == "agent.identity_decision"
    assert "user must be non-empty" in report["findings"][0]["message"]
    assert [call["role"] for call in agent.calls] == [
        "image_creator",
        "image_creator",
        "testcase_creator",
        "testcase_qa",
    ]


def test_missing_image_creator_contract_joins_existing_precheck_repair(tmp_path):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    workspace.mkdir()
    initial = {
        "success": True,
        "files_created": ["Bigdata/kylin-e2e-test/meta.yml"],
    }
    repaired = {
        **initial,
        "identity_decision": {
            "mode": "reuse_existing",
            "user": "root",
            "group": "root",
            "uid": None,
            "gid": None,
        },
    }
    agent = StubAgent(
        {
            "image_creator": [initial, repaired],
            "testcase_creator": [_testcase_creator_output()],
            "testcase_qa": [_approved_tests()],
        }
    )

    run_generation_pipeline(
        workspace=workspace,
        report_dir=tmp_path / "evidence",
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=lambda **_: {"status": "passed"},
    )

    assert [call["role"] for call in agent.calls].count("image_creator") == 2
    report = json.loads(
        (tmp_path / "evidence" / "image-precheck-gates.json").read_text()
    )
    assert report["findings"][0]["code"] == "agent.identity_decision"
    assert "missing required keys" in report["findings"][0]["message"]


def test_testcase_qa_receives_latest_evidence_after_precheck_repair(tmp_path):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    initial = _testcase_creator_output(
        command_evidence=[
            {
                "command": "redis-cli PING",
                "semantics": "returns PONG",
                "evidence_id": "command-ping-initial",
            }
        ],
        evidence=[
            {
                "id": "command-ping-initial",
                "claim": "PING returns PONG",
                "source": (
                    "https://github.com/apache/kvrocks/blob/"
                    "v2.16.0/src/commands/cmd_server.cc"
                ),
                "excerpts": ["Ping::Execute"],
            }
        ],
    )
    repaired = _testcase_creator_output(
        command_evidence=[
            {
                "command": "redis-cli EXISTS evidence-key",
                "semantics": "returns whether the exact key exists",
                "evidence_id": "command-exists-repaired",
            }
        ],
        evidence=[
            {
                "id": "command-exists-repaired",
                "claim": "EXISTS reports whether the exact key exists",
                "source": (
                    "https://github.com/apache/kvrocks/blob/"
                    "v2.16.0/src/commands/cmd_key.cc"
                ),
                "excerpts": ["Exists::Execute"],
            }
        ],
    )
    agent = StubAgent(
        {
            "image_creator": [_image_creator_output()],
            "testcase_creator": [initial, repaired],
            "testcase_qa": [_approved_tests()],
        }
    )
    full_results = iter(
        (
            _repairable_gate(
                owner="testcase_creator",
                code="tests.shell_syntax",
                message="test.sh needs repair",
            ),
            {"status": "passed"},
            {"status": "passed"},
        )
    )

    def validator(*, phase, **_):
        if phase == "image":
            return {"status": "passed"}
        return next(full_results)

    run_generation_pipeline(
        workspace=workspace,
        report_dir=reports,
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=validator,
    )

    testcase_qa_prompt = next(
        call["prompt"] for call in agent.calls if call["role"] == "testcase_qa"
    )
    assert "redis-cli EXISTS evidence-key" in testcase_qa_prompt
    assert "command-exists-repaired" in testcase_qa_prompt
    assert "command-ping-initial" not in testcase_qa_prompt


def test_generation_redacts_secret_from_one_line_qa_summary(tmp_path, capsys):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    agent = _fully_approved_agent(
        testcase_summary="approved\nwithout deepseek-secret",
    )

    run_generation_pipeline(
        workspace=workspace,
        report_dir=reports,
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=lambda **_: {"status": "passed"},
    )

    output = capsys.readouterr().out
    assert "deepseek-secret" not in output
    assert 'summary="approved without REDACTED"' in output


def test_generation_records_failed_target_gate_before_raising(tmp_path):
    from scripts.lib.generation_pipeline import (
        GenerationPipelineError,
        run_generation_pipeline,
    )

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    agent = _fully_approved_agent()
    failed_gate = {
        "status": "failed",
        "errors": ["unexpected target path"],
    }
    gate_results = iter(
        ({"status": "passed"}, {"status": "passed"}, failed_gate)
    )

    with pytest.raises(
        GenerationPipelineError,
        match="deterministic target contract",
    ):
        run_generation_pipeline(
            workspace=workspace,
            report_dir=reports,
            task=_task(),
            base_sha="1" * 40,
            executable=tmp_path / "opencode",
            api_key="deepseek-secret",
            agent_runner=agent,
            target_validator=lambda **_: next(gate_results),
        )

    assert json.loads((reports / "gates.json").read_text()) == failed_gate


def test_generation_full_hard_stop_fails_before_testcase_qa(tmp_path):
    from scripts.lib.generation_pipeline import (
        GenerationPipelineError,
        run_generation_pipeline,
    )

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    agent = _fully_approved_agent()
    from scripts.lib.target_contract import TargetContractError

    def failed_validator(*, phase, **_):
        if phase == "image":
            return {"status": "passed", "phase": phase}
        raise TargetContractError(
            "meta.yml is not valid YAML",
            findings=[
                {
                    "code": "meta.invalid_yaml",
                    "level": "hard_stop",
                    "owner": "workflow",
                    "message": "meta.yml is not valid YAML",
                }
            ],
        )

    with pytest.raises(
        GenerationPipelineError,
        match="deterministic target precheck",
    ):
        run_generation_pipeline(
            workspace=workspace,
            report_dir=reports,
            task=_task(),
            base_sha="1" * 40,
            executable=tmp_path / "opencode",
            api_key="deepseek-secret",
            agent_runner=agent,
            target_validator=failed_validator,
        )

    assert [call["role"] for call in agent.calls] == [
        "image_creator",
        "testcase_creator",
    ]
    assert json.loads((reports / "precheck-gates.json").read_text()) == {
        "build_allowed": False,
        "delivery_allowed": False,
        "errors": ["meta.yml is not valid YAML"],
        "findings": [
            {
                "code": "meta.invalid_yaml",
                "level": "hard_stop",
                "message": "meta.yml is not valid YAML",
                "owner": "workflow",
            }
        ],
        "status": "failed",
        "test_allowed": False,
    }
    assert not (reports / "precheck-repair-gates.json").exists()


def test_generation_records_agent_timeout_without_exposing_secret(tmp_path):
    from scripts.lib.agent_runtime import AgentRuntimeError
    from scripts.lib.generation_pipeline import (
        GenerationPipelineError,
        run_generation_pipeline,
    )

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()

    def timeout_agent(**kwargs):
        raise AgentRuntimeError(
            f"{kwargs['role']} timed out with deepseek-secret"
        )

    with pytest.raises(GenerationPipelineError, match="image_creator"):
        run_generation_pipeline(
            workspace=workspace,
            report_dir=reports,
            task=_task(),
            base_sha="1" * 40,
            executable=tmp_path / "opencode",
            api_key="deepseek-secret",
            agent_runner=timeout_agent,
            target_validator=lambda **_: {"status": "passed"},
        )

    assert json.loads((reports / "generation-failure.json").read_text()) == {
        "error": "image_creator timed out with REDACTED",
        "role": "image_creator",
        "stage": "agent",
        "status": "failed",
    }


def test_generation_records_unsuccessful_creator_payload_before_raising(
    tmp_path,
):
    from scripts.lib.agent_runtime import AgentResult
    from scripts.lib.generation_pipeline import (
        GenerationPipelineError,
        run_generation_pipeline,
    )

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()

    def failed_creator(**kwargs):
        return AgentResult(
            role=kwargs["role"],
            payload={"success": False, "files_created": [], "error": "failed"},
        )

    with pytest.raises(GenerationPipelineError, match="image_creator"):
        run_generation_pipeline(
            workspace=workspace,
            report_dir=reports,
            task=_task(),
            base_sha="1" * 40,
            executable=tmp_path / "opencode",
            api_key="deepseek-secret",
            agent_runner=failed_creator,
            target_validator=lambda **_: {"status": "passed"},
        )

    assert json.loads((reports / "image-creator.json").read_text()) == {
        "error": "failed",
        "files_created": [],
        "success": False,
    }


def test_generation_fails_closed_on_unsuccessful_initial_testcase_creator(
    tmp_path,
):
    from scripts.lib.generation_pipeline import (
        GenerationPipelineError,
        run_generation_pipeline,
    )

    workspace = tmp_path / "target"
    workspace.mkdir()
    agent = StubAgent(
        {
            "image_creator": [_image_creator_output()],
            "testcase_creator": [
                _testcase_creator_output(success=False),
            ],
        }
    )

    with pytest.raises(
        GenerationPipelineError,
        match="testcase_creator did not complete successfully",
    ):
        run_generation_pipeline(
            workspace=workspace,
            report_dir=tmp_path / "evidence",
            task=_task(),
            base_sha="1" * 40,
            executable=tmp_path / "opencode",
            api_key="deepseek-secret",
            agent_runner=agent,
            target_validator=lambda **_: {"status": "passed"},
        )


def test_generation_fails_closed_on_unsuccessful_testcase_qa_repair(tmp_path):
    from scripts.lib.generation_pipeline import (
        GenerationPipelineError,
        run_generation_pipeline,
    )

    workspace = tmp_path / "target"
    workspace.mkdir()
    agent = StubAgent(
        {
            "image_creator": [_image_creator_output()],
            "testcase_creator": [
                _testcase_creator_output(),
                _testcase_creator_output(success=False),
            ],
            "testcase_qa": [
                {
                    "status": "needs_fix",
                    "issues": [_test_issue()],
                    "coverage_score": 0.7,
                    "summary": "repair tests",
                }
            ],
        }
    )

    with pytest.raises(
        GenerationPipelineError,
        match="testcase_creator repair failed",
    ):
        run_generation_pipeline(
            workspace=workspace,
            report_dir=tmp_path / "evidence",
            task=_task(),
            base_sha="1" * 40,
            executable=tmp_path / "opencode",
            api_key="deepseek-secret",
            agent_runner=agent,
            target_validator=lambda **_: {"status": "passed"},
        )


def test_generation_fails_closed_on_unsuccessful_deterministic_repair(tmp_path):
    from scripts.lib.generation_pipeline import (
        GenerationPipelineError,
        run_generation_pipeline,
    )

    workspace = tmp_path / "target"
    workspace.mkdir()
    agent = StubAgent(
        {
            "image_creator": [
                _image_creator_output(),
                _image_creator_output(success=False),
            ],
        }
    )

    with pytest.raises(
        GenerationPipelineError,
        match="image_creator deterministic repair failed",
    ):
        run_generation_pipeline(
            workspace=workspace,
            report_dir=tmp_path / "evidence",
            task=_task(),
            base_sha="1" * 40,
            executable=tmp_path / "opencode",
            api_key="deepseek-secret",
            agent_runner=agent,
            target_validator=lambda **_: _repairable_gate(
                owner="image_creator",
                code="readme.section",
                message="README.md is missing section: # Usage",
            ),
        )


def test_testcase_qa_prompt_embeds_candidate_snapshot_without_tool_reads(tmp_path):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    image = (
        workspace
        / "Database"
        / "kvrocks"
        / "2.16.0"
        / "24.03-lts-sp4"
    )
    tests = workspace / "Database" / "kvrocks" / "tests"
    image.mkdir(parents=True)
    tests.mkdir(parents=True)
    (image / "Dockerfile").write_text(
        "FROM openEuler\n"
        "RUN groupadd -r kvrocks && useradd -r -g kvrocks kvrocks\n"
        "USER kvrocks\n"
    )
    (tests / "test.sh").write_text("redis-cli -p 6666 PING\n")
    agent = _fully_approved_agent()

    run_generation_pipeline(
        workspace=workspace,
        report_dir=reports,
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=lambda **_: {"status": "passed"},
    )

    testcase_qa_prompt = agent.calls[2]["prompt"]
    assert "Embedded candidate snapshot" in testcase_qa_prompt
    assert "Do not call tools" in testcase_qa_prompt
    assert "Database/kvrocks/2.16.0/24.03-lts-sp4/Dockerfile" in testcase_qa_prompt
    assert "FROM openEuler" in testcase_qa_prompt
    assert "Database/kvrocks/2.16.0/24.03-lts-sp4/test.sh" not in (
        testcase_qa_prompt
    )
    assert "Database/kvrocks/tests/test.sh" in testcase_qa_prompt
    assert "redis-cli -p 6666 PING" in testcase_qa_prompt


def test_testcase_qa_does_not_inherit_removed_image_semantic_review(tmp_path):
    auxiliary_root = (
        tmp_path
        / "target"
        / "Database"
        / "kvrocks"
        / "2.16.0"
        / "24.03-lts-sp4"
    )
    agent = _fully_approved_agent()

    def write_candidate(role, _):
        if role != "image_creator":
            return
        auxiliary_root.mkdir(parents=True)
        (auxiliary_root / "Dockerfile").write_text(
            "FROM openEuler\n"
            "RUN groupadd -r kvrocks && useradd -r -g kvrocks kvrocks\n"
            "COPY service.conf /etc/example/service.conf\n"
            "USER kvrocks\n"
        )
        (auxiliary_root / "service.conf").write_text("listen = 0.0.0.0\n")
        (auxiliary_root / "entrypoint.sh").write_text("#!/bin/sh\nexec example\n")
        tests_root = auxiliary_root.parents[1] / "tests"
        tests_root.mkdir()
        (tests_root / "test.sh").write_text("test candidate image\n")
        results_root = auxiliary_root.parents[1] / "results"
        results_root.mkdir()
        (results_root / "results.json").write_text('{"status": "passed"}\n')

    _run_recorded_pipeline(
        tmp_path,
        agent,
        mutation=write_candidate,
    )

    testcase_qa_prompt = agent.calls[2]["prompt"]
    assert "Database/kvrocks/2.16.0/24.03-lts-sp4/service.conf" not in testcase_qa_prompt
    assert "listen = 0.0.0.0" not in testcase_qa_prompt
    assert "Database/kvrocks/2.16.0/24.03-lts-sp4/entrypoint.sh" not in testcase_qa_prompt
    assert "Database/kvrocks/tests/test.sh" in testcase_qa_prompt
    assert "Database/kvrocks/results/results.json" not in testcase_qa_prompt


def test_testcase_role_owns_nested_shared_test_assets_and_qa_reads_them(tmp_path):
    agent = _fully_approved_agent()
    fixture = (
        tmp_path
        / "target"
        / "Database"
        / "kvrocks"
        / "tests"
        / "fixtures"
        / "protocol.txt"
    )

    def write_nested_fixture(role, _):
        if role == "testcase_creator":
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text("PING => PONG\n")

    _run_recorded_pipeline(tmp_path, agent, mutation=write_nested_fixture)

    testcase_qa_prompt = next(
        call["prompt"] for call in agent.calls if call["role"] == "testcase_qa"
    )
    assert "Database/kvrocks/tests/fixtures/protocol.txt" in testcase_qa_prompt
    assert "PING => PONG" in testcase_qa_prompt


def test_qa_snapshot_compacts_large_files_and_continues_review(tmp_path):
    dockerfile = (
        tmp_path
        / "target"
        / "Database"
        / "kvrocks"
        / "2.16.0"
        / "24.03-lts-sp4"
        / "Dockerfile"
    )
    agent = _fully_approved_agent()

    def write_oversized_candidate(role, _):
        if role == "image_creator":
            dockerfile.parent.mkdir(parents=True)
            dockerfile.write_text(
                "FROM openEuler\n"
                "RUN groupadd -r kvrocks && useradd -r -g kvrocks kvrocks\n"
                "RUN printf 'HEAD-MARKER\n"
                + "X" * 70_000
                + "\nTAIL-MARKER' >/snapshot-fixture\n"
                "USER kvrocks\n"
            )

    events, reports = _run_recorded_pipeline(
        tmp_path,
        agent,
        mutation=write_oversized_candidate,
    )

    assert "agent:testcase_qa" in events
    testcase_qa_prompt = next(
        call["prompt"] for call in agent.calls if call["role"] == "testcase_qa"
    )
    assert len(testcase_qa_prompt) <= 100_000
    assert "compacted text file" in testcase_qa_prompt
    assert "HEAD-MARKER" in testcase_qa_prompt
    assert "TAIL-MARKER" in testcase_qa_prompt
    assert "sha256" in testcase_qa_prompt
    assert not (reports / "generation-failure.json").exists()
    qa_report = json.loads((reports / "testcase-qa-round1.json").read_text())
    snapshot = qa_report["harness"]["snapshot"]
    assert snapshot["status"] == "compacted"
    assert snapshot["complete_text"] is False
    assert any(path.endswith("Dockerfile") for path in snapshot["compacted_files"])


def test_non_png_binary_is_hashed_without_decoding_into_qa_prompt(tmp_path):
    from scripts.lib.generation_pipeline import _qa_prompt

    workspace = tmp_path / "target"
    app = workspace / "Database" / "kvrocks" / "tests" / "fixtures"
    app.mkdir(parents=True)
    (app / "payload.tar").write_bytes(b"\0" * 70_000)

    prompt, snapshot = _qa_prompt(
        role="testcase_qa",
        workspace=workspace,
        task=_task(),
        base_sha="1" * 40,
        return_snapshot=True,
    )

    assert "<binary file: 70000 bytes" in prompt
    assert len(prompt) <= 100_000
    assert snapshot["hashed_binary_files"] == [
        "Database/kvrocks/tests/fixtures/payload.tar"
    ]


def test_generation_reports_must_be_outside_target_workspace(tmp_path):
    from scripts.lib.generation_pipeline import (
        GenerationPipelineError,
        run_generation_pipeline,
    )

    workspace = tmp_path / "target"
    workspace.mkdir()

    with pytest.raises(GenerationPipelineError, match="outside"):
        run_generation_pipeline(
            workspace=workspace,
            report_dir=workspace / "reports",
            task=_task(),
            base_sha="1" * 40,
            executable=tmp_path / "opencode",
            api_key="deepseek-secret",
            agent_runner=StubAgent({}),
            target_validator=lambda **_: {},
        )


def test_unavailable_testcase_evidence_reaches_qa_without_a_repair_round(tmp_path):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    workspace.mkdir()
    agent = _fully_approved_agent()
    testcase_payload = agent.responses["testcase_creator"][0]
    resolver_calls = []

    def unavailable_resolver(**kwargs):
        resolver_calls.append(kwargs)
        return {
            "status": "unavailable",
            "reason": "source could not be fetched",
            "entries": [],
        }

    result = run_generation_pipeline(
        workspace=workspace,
        report_dir=tmp_path / "reports",
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=lambda **_: {"status": "passed"},
        evidence_resolver=unavailable_resolver,
    )

    assert result.status == "passed"
    assert len(resolver_calls) >= 1
    assert resolver_calls[0]["evidence"] == testcase_payload["evidence"]
    testcase_qa_call = next(
        call for call in agent.calls if call["role"] == "testcase_qa"
    )
    assert '"status": "unavailable"' in testcase_qa_call["prompt"]
    assert [call["role"] for call in agent.calls].count("testcase_creator") == 1


def test_generation_accepts_a_task_it_has_never_seen(tmp_path):
    """Generation carries no application knowledge, so it must not gate on one.

    The prompts and the target gates were made application-neutral in 5eb80d6;
    the remaining Kvrocks assumptions live in native validation, and refusing
    the TaskSpec here only hid that fact behind an earlier error.
    """
    from scripts.lib.generation_pipeline import run_generation_pipeline
    from scripts.lib.task_spec import TaskSpec

    workspace = tmp_path / "target"
    workspace.mkdir()
    agent = _fully_approved_agent()
    agent.responses["image_creator"] = [
        {
            "success": True,
            "files_created": ["Cloud/caddy/meta.yml"],
            "identity_decision": {
                "mode": "dynamic",
                "user": "caddy",
                "group": "caddy",
                "uid": None,
                "gid": None,
                "requirement_evidence_ids": [],
            },
            "evidence": [],
        }
    ]
    # The QA prompt echoes the Creator's command evidence, so the fixture has
    # to speak about the task under test rather than the default one.
    agent.responses["testcase_creator"] = [
        _testcase_creator_output(
            files_created=["Cloud/caddy/tests/test.sh"],
            command_evidence=[
                {
                    "command": "caddy version",
                    "semantics": "prints the compiled release string",
                    "evidence_id": "command-version-001",
                }
            ],
            evidence=[
                {
                    "id": "command-version-001",
                    "claim": "caddy version prints the compiled release string",
                    "source": (
                        "https://github.com/caddyserver/caddy/blob/"
                        "v2.11.4/cmd/commandfuncs.go"
                    ),
                    "excerpts": ["cmdVersion"],
                }
            ],
        )
    ]
    unseen = TaskSpec.from_workflow_dispatch(
        {
            "app": "caddy",
            "version": "2.11.4",
            "os_version": "24.03-lts-sp4",
            "domain": "Cloud",
            "source_url": "https://github.com/caddyserver/caddy/tree/v2.11.4",
        }
    )

    result = run_generation_pipeline(
        workspace=workspace,
        report_dir=tmp_path / "evidence",
        task=unseen,
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=lambda **_: {"status": "passed"},
    )

    assert result.status == "passed"
    assert [call["role"] for call in agent.calls][0] == "image_creator"
    for call in agent.calls:
        assert "kvrocks" not in call["prompt"].lower()


def test_the_smoke_candidate_still_refuses_a_task_it_cannot_write(tmp_path):
    """write_smoke_candidate emits Kvrocks literals, so it stays pinned."""
    from scripts.lib.generation_pipeline import (
        GenerationPipelineError,
        write_smoke_candidate,
    )
    from scripts.lib.task_spec import TaskSpec

    workspace = tmp_path / "target"
    workspace.mkdir()
    unsupported = TaskSpec.from_workflow_dispatch(
        {
            "app": "redis",
            "version": "8.0.0",
            "os_version": "24.03-lts-sp4",
            "domain": "Database",
            "source_url": "https://github.com/redis/redis/tree/8.0.0",
        }
    )

    with pytest.raises(GenerationPipelineError, match="smoke candidate"):
        write_smoke_candidate(workspace=workspace, task=unsupported)


def test_smoke_image_info_uses_the_upstream_format_block_style(tmp_path):
    from scripts.lib.generation_pipeline import write_smoke_candidate

    workspace = tmp_path / "target"
    domain = workspace / "Database"
    domain.mkdir(parents=True)
    (domain / "image-list.yml").write_text("images: {}\n")

    write_smoke_candidate(workspace=workspace, task=_task())

    content = (
        workspace / "Database" / "kvrocks" / "doc" / "image-info.yml"
    ).read_text()
    for key in ("environment", "tags", "download", "usage"):
        assert f"{key}: |\n" in content


def test_phase1_prompts_pin_task_paths_without_injecting_app_implementation():
    from scripts.lib.generation_pipeline import build_role_prompt

    prompt = build_role_prompt(
        role="image_creator",
        task=_task(),
        base_sha="1" * 40,
    )

    assert "Database/kvrocks" in prompt
    assert "2.16.0/24.03-lts-sp4/Dockerfile" in prompt
    assert "Database/image-list.yml" in prompt
    assert "Do not modify any other path" in prompt
    assert "Do not create workflow control assets" in prompt
    assert "official upstream" in prompt
    for fragment in (
        "./x.py build",
        "-j 4",
        "UID/GID 999",
        "TCP 6666",
        "Redis-protocol PING",
        "redis-cli -p 6666 PING",
        "/dev/tcp",
        "restart persistence",
        "--non-unique",
        "libatomic",
    ):
        assert fragment not in prompt
    assert "Do not install or upgrade host tools or packages" in prompt
    assert "Do not compile or build the target application" in prompt
    assert "directly on the Runner or inside `docker run`" in prompt
    assert "bounded, read-only inspection of the TaskSpec base image" in prompt
    assert (
        "Write the minimum complete candidate before optional research" in prompt
    )
    assert "leave uncertain facts to `native_build` and `runtime_test`" in prompt
    # Run 30567356119 unpacked an upstream tarball into the target repo, so
    # every role must be told where research output belongs instead.
    assert ".oe-scratch" in prompt
    assert "downloads, archives and temporary files" in prompt
    assert "Your final response MUST be exactly one JSON object" in prompt
    assert (
        "documented `success`, `files_created`, and `identity_decision` keys"
    ) in prompt
    assert "tool output is not the final response" in prompt
    assert "deepseek-secret" not in prompt

    testcase_prompt = build_role_prompt(
        role="testcase_creator",
        task=_task(),
        base_sha="1" * 40,
    )
    assert "runtime_test" in testcase_prompt
    assert "executes the shared test.sh exactly once" in testcase_prompt
    assert "must not invoke Docker" in testcase_prompt
    assert "Database/kvrocks/tests/test.sh" in testcase_prompt
    assert "runtime image" in testcase_prompt
    assert "image-list, Dockerfile, metadata" in testcase_prompt
    assert "read-only" in testcase_prompt
    assert "final Dockerfile" in testcase_prompt
    for fragment in ("redis-cli", "6666", "UID 999", "kvrocks --version"):
        assert fragment not in testcase_prompt

    fixer_prompt = build_role_prompt(
        role="fixer",
        task=_task(),
        base_sha="1" * 40,
    )
    assert "runtime_test" in fixer_prompt
    assert "observable runtime contract" in fixer_prompt
    assert "dependent candidate files" in fixer_prompt


def test_test_roles_have_one_runtime_test_contract():
    from scripts.lib.generation_pipeline import build_role_prompt

    for role in ("testcase_creator", "testcase_qa", "fixer"):
        prompt = build_role_prompt(
            role=role,
            task=_task(),
            base_sha="1" * 40,
        ).lower()
        assert "runtime_test" in prompt
        assert "tests/test.sh" in prompt


def test_smoke_candidate_generates_only_native_test_assets(tmp_path):
    from scripts.lib.generation_pipeline import write_smoke_candidate

    workspace = tmp_path / "target"
    (workspace / "Database").mkdir(parents=True)
    (workspace / "Database" / "image-list.yml").write_text("images: {}\n")

    write_smoke_candidate(workspace=workspace, task=_task())

    tests = workspace / "Database" / "kvrocks" / "tests"
    assert sorted(path.name for path in tests.iterdir()) == ["test.sh"]
    script = (tests / "test.sh").read_text()
    assert 'test "${reported_version}" = "${EXPECTED_VERSION}"' in script
    assert 'test "${ping}" = "PONG"' in script


def test_shared_prompt_contract_does_not_inject_kvrocks_rules():
    from scripts.lib.generation_pipeline import build_role_prompt
    from scripts.lib.task_spec import TaskSpec

    task = TaskSpec.from_workflow_dispatch(
        {
            "app": "clickhouse",
            "version": "25.7.5.34",
            "os_version": "24.03-lts-sp2",
            "domain": "Database",
            "source_url": (
                "https://github.com/ClickHouse/ClickHouse/"
                "tree/v25.7.5.34-stable"
            ),
        }
    )

    image_prompt = build_role_prompt(
        role="image_creator",
        task=task,
        base_sha="1" * 40,
    )
    assert f"Pinned source URL: `{task.source_url}`" in image_prompt
    for fragment in (
        "Kvrocks",
        "redis-cli",
        "6666",
        "./x.py build",
        "UID/GID 999",
        "libatomic",
    ):
        assert fragment not in image_prompt

    for role in ("testcase_creator", "testcase_qa", "fixer"):
        prompt = build_role_prompt(
            role=role,
            task=task,
            base_sha="1" * 40,
        )
        assert "runtime_test" in prompt
        for fragment in ("Kvrocks", "redis-cli", "6666", "./x.py build"):
            assert fragment not in prompt


def test_fixer_prompt_whitelists_generated_candidate_files(tmp_path):
    from scripts.lib.generation_pipeline import build_role_prompt

    workspace = tmp_path / "target"
    image_root = (
        workspace
        / "Database"
        / "kvrocks"
        / "2.16.0"
        / "24.03-lts-sp4"
    )
    image_root.mkdir(parents=True)
    (image_root / "Dockerfile").write_text("FROM openEuler\n")
    (image_root / "service.conf").write_text("listen = 0.0.0.0\n")
    (image_root / "entrypoint.sh").write_text("#!/bin/sh\n")
    results_root = workspace / "Database" / "kvrocks" / "results"
    results_root.mkdir()
    (results_root / "results.json").write_text('{"status": "passed"}\n')

    prompt = build_role_prompt(
        role="fixer",
        task=_task(),
        base_sha="1" * 40,
        workspace=workspace,
    )

    assert "Fixer whitelist (only these files may be modified)" in prompt
    assert "Database/image-list.yml" in prompt
    assert "Database/kvrocks/2.16.0/24.03-lts-sp4/Dockerfile" in prompt
    assert "Database/kvrocks/2.16.0/24.03-lts-sp4/service.conf" in prompt
    assert "Database/kvrocks/2.16.0/24.03-lts-sp4/entrypoint.sh" in prompt
    assert "Database/kvrocks/2.16.0/24.03-lts-sp4/test.sh" not in prompt
    assert "Database/kvrocks/tests/test.sh" in prompt
    assert "Database/kvrocks/tests/test_helpers.sh" in prompt
    assert "Database/kvrocks/results/results.json" not in prompt


def test_generation_hard_stops_on_stray_tarball_outside_task_scope(
    tmp_path,
):
    """Run 30567356119 failed here, not in native repair.

    The Creator unpacked an upstream tarball into the target repo and the gate
    answered with 496 "change outside task scope" errors, whose obvious repair
    is to revert the candidate rather than remove the tarball.
    """
    from scripts.lib.generation_pipeline import (
        GenerationPipelineError,
        run_generation_pipeline,
    )

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    created = _image_creator_output()
    agent = StubAgent(
        {
            "image_creator": [created],
            "testcase_creator": [_testcase_creator_output()],
            "testcase_qa": [_approved_tests()],
        }
    )
    failed = {
        "status": "failed",
        "build_allowed": False,
        "delivery_allowed": False,
        "test_allowed": False,
        "errors": [
            "change outside task scope or wrong status: A "
            "kvrocks-2.16.0/CMakeLists.txt",
            "change outside task scope or wrong status: A "
            "kvrocks-2.16.0/src/cli/main.cc",
        ],
        "findings": [
            {
                "code": "scope.outside_task",
                "level": "hard_stop",
                "owner": "workflow",
                "message": "change outside task scope",
            }
        ],
    }

    def validator(*, phase, **_):
        if phase == "image":
            return failed
        return {"status": "passed"}

    with pytest.raises(GenerationPipelineError, match="image precheck"):
        run_generation_pipeline(
            workspace=workspace,
            report_dir=reports,
            task=_task(),
            base_sha="1" * 40,
            executable=tmp_path / "opencode",
            api_key="deepseek-secret",
            agent_runner=agent,
            target_validator=validator,
        )

    assert [call["role"] for call in agent.calls] == ["image_creator"]
    assert not (reports / "image-creator-precheck-repair.json").exists()


def test_testcase_qa_prompt_carries_the_creator_command_evidence(tmp_path):
    """QA has to be able to check the claim, not re-derive it.

    In run 30781977554 the reviewer approved a `DBSIZE` assertion written with
    Redis semantics at coverage 0.9. It had no statement of where any
    expectation came from, so there was nothing to check.
    """
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    tests = workspace / "Database" / "kvrocks" / "tests"
    tests.mkdir(parents=True)
    (tests / "test.sh").write_text("redis-cli -p 6666 DBSIZE\n")
    agent = _fully_approved_agent()
    agent.responses["testcase_creator"] = [
        _testcase_creator_output(
            command_evidence=[
                {
                    "command": "redis-cli DBSIZE",
                    "semantics": (
                        "returns a cached count refreshed by DBSIZE SCAN"
                    ),
                    "evidence_id": "command-dbsize-001",
                }
            ],
            evidence=[
                {
                    "id": "command-dbsize-001",
                    "claim": "DBSIZE returns a cached count",
                    "source": (
                        "https://github.com/apache/kvrocks/blob/"
                        "v2.16.0/src/commands/cmd_server.cc"
                    ),
                    "excerpts": ["DBSize::Execute"],
                }
            ],
        )
    ]

    fixed = {
        "status": "available",
        "entries": [
            {
                "id": "command-dbsize-001",
                "fetch_status": "available",
                "excerpt_checks": [
                    {
                        "index": 0,
                        "found": True,
                        "context": "DBSize::Execute reads the cached key count",
                    }
                ],
                "sha256": "b" * 64,
            }
        ],
    }

    run_generation_pipeline(
        workspace=workspace,
        report_dir=tmp_path / "evidence",
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=lambda **_: {"status": "passed"},
        evidence_resolver=lambda **_: fixed,
    )

    testcase_qa_prompt = agent.calls[2]["prompt"]
    assert "Testcase Creator command evidence" in testcase_qa_prompt
    assert "refreshed by DBSIZE SCAN" in testcase_qa_prompt
    assert "Harness-fixed Creator evidence bundle" in testcase_qa_prompt
    assert "DBSize::Execute reads the cached key count" in testcase_qa_prompt
    assert "Record an actual candidate concern" not in testcase_qa_prompt


def test_testcase_qa_receives_harness_fixed_evidence_bundle(tmp_path):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    tests = workspace / "Database" / "kvrocks" / "tests"
    tests.mkdir(parents=True)
    (tests / "test.sh").write_text("redis-cli -p 6666 EXISTS evidence-key\n")
    agent = _fully_approved_agent()
    agent.responses["testcase_creator"] = [
        _testcase_creator_output(
            command_evidence=[
                {
                    "command": "redis-cli EXISTS evidence-key",
                    "semantics": "returns whether the exact key exists",
                    "evidence_id": "command-exists-001",
                }
            ],
            evidence=[
                {
                    "id": "command-exists-001",
                    "claim": "EXISTS reports whether the exact key exists",
                    "source": (
                        "https://github.com/apache/kvrocks/blob/"
                        "v2.16.0/src/commands/cmd_key.cc"
                    ),
                    "excerpts": ["Exists::Execute"],
                }
            ]
        )
    ]
    fixed = {
        "status": "available",
        "scenario": "new-image",
        "entries": [
            {
                "id": "command-exists-001",
                "fetch_status": "available",
                "excerpt_checks": [
                    {
                        "index": 0,
                        "found": True,
                        "context": "Exists::Execute returns the number of keys found",
                    }
                ],
                "sha256": "a" * 64,
            }
        ],
    }
    resolver_calls = []

    def resolver(*, task, evidence):
        resolver_calls.append((task, evidence))
        return fixed

    run_generation_pipeline(
        workspace=workspace,
        report_dir=tmp_path / "evidence",
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=lambda **_: {"status": "passed"},
        evidence_resolver=resolver,
    )

    testcase_qa_prompt = next(
        call["prompt"] for call in agent.calls if call["role"] == "testcase_qa"
    )
    assert "Harness-fixed Creator evidence bundle" in testcase_qa_prompt
    assert "Exists::Execute returns the number of keys found" in testcase_qa_prompt
    assert resolver_calls[-1][1][0]["id"] == "command-exists-001"
    assert json.loads(
        (
            tmp_path
            / "evidence"
            / "testcase-round1-evidence-bundle.json"
        ).read_text()
    ) == fixed


def test_testcase_creator_contract_error_joins_the_existing_precheck_repair(
    tmp_path,
):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    workspace.mkdir()
    initial = _testcase_creator_output(command_evidence=[])
    repaired = _testcase_creator_output()
    agent = StubAgent(
        {
            "image_creator": [_fully_approved_agent().responses["image_creator"][0]],
            "testcase_creator": [initial, repaired],
            "testcase_qa": [_approved_tests()],
        }
    )

    run_generation_pipeline(
        workspace=workspace,
        report_dir=tmp_path / "evidence",
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=lambda **_: {"status": "passed"},
    )

    assert [call["role"] for call in agent.calls].count("testcase_creator") == 2
    report = json.loads(
        (tmp_path / "evidence" / "precheck-gates.json").read_text()
    )
    assert report["delivery_allowed"] is False
    assert report["findings"][0]["owner"] == "testcase_creator"
    assert report["findings"][0]["code"] == "agent.command_evidence"
    assert "non-empty list" in report["findings"][0]["message"]


def test_unavailable_creator_evidence_continues_to_qa_judgment(tmp_path):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    workspace.mkdir()
    agent = _fully_approved_agent()
    unavailable = {
        "status": "unavailable",
        "entries": [
            {
                "id": "command-ping-001",
                "fetch_status": "unavailable",
                "reason": "source could not be fetched",
            }
        ],
    }

    result = run_generation_pipeline(
        workspace=workspace,
        report_dir=tmp_path / "evidence",
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=lambda **_: {"status": "passed"},
        evidence_resolver=lambda **_: unavailable,
    )

    assert result.status == "passed"
    assert "testcase_qa" in [call["role"] for call in agent.calls]
    assert json.loads(
        (
            tmp_path
            / "evidence"
            / "testcase-round1-evidence-bundle.json"
        ).read_text()
    ) == unavailable


def test_evidence_only_needs_fix_does_not_start_a_creator_repair(tmp_path):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    workspace.mkdir()
    agent = _fully_approved_agent()
    agent.responses["testcase_qa"] = [
        {
            "status": "needs_fix",
            "issues": [],
            "evidence_reviews": [
                {
                    "evidence_id": "command-ping-001",
                    "status": "unavailable",
                    "reason": "source could not be fetched",
                }
            ],
            "summary": "candidate is sound but evidence is unavailable",
        },
    ]

    result = run_generation_pipeline(
        workspace=workspace,
        report_dir=tmp_path / "evidence",
        task=_task(),
        base_sha="1" * 40,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=lambda **_: {"status": "passed"},
    )

    roles = [call["role"] for call in agent.calls]
    assert result.status == "passed"
    assert result.qa_fix_rounds == 0
    assert roles.count("testcase_creator") == 1
    assert roles.count("testcase_qa") == 1


def test_large_evidence_remains_reviewable_in_the_bounded_qa_prompt(tmp_path):
    from scripts.lib.generation_pipeline import _qa_prompt

    workspace = tmp_path / "target"
    tests = workspace / "Database" / "kvrocks" / "tests"
    tests.mkdir(parents=True)
    (tests / "test.sh").write_text("#" + "c" * 59_000)

    creator_evidence = [
        {
            "id": f"evidence-{index}",
            "claim": f"claim-{index} " + "x" * 500,
            "source": (
                "https://github.com/apache/kvrocks/blob/"
                f"v2.16.0/docs/evidence-{index}.md"
            ),
            "excerpts": [
                f"excerpt-{index}-{excerpt} " + "y" * 490
                for excerpt in range(2)
            ],
        }
        for index in range(6)
    ]
    fixed_entries = [
        {
            **creator_evidence[index],
            "id": f"evidence-{index}",
            "fetch_status": "available",
            "sha256": str(index) * 64,
            "excerpt_checks": [
                {
                    "index": excerpt,
                    "found": True,
                    "match_method": "exact",
                    "context": f"context-{index}-{excerpt} " + "z" * 240,
                }
                for excerpt in range(2)
            ],
        }
        for index in range(6)
    ]

    prompt = _qa_prompt(
        role="testcase_qa",
        workspace=workspace,
        task=_task(),
        base_sha="1" * 40,
        creator_payload={
            "success": True,
            "files_created": ["Database/kvrocks/tests/test.sh"],
            "command_evidence": [
                {
                    "command": f"command-{index}",
                    "semantics": f"semantics-{index}",
                    "evidence_id": f"evidence-{index}",
                }
                for index in range(6)
            ],
            "evidence": creator_evidence,
        },
        evidence_bundle={
            "status": "available",
            "entries": fixed_entries,
        },
    )

    assert len(prompt) <= 100_000
    assert "compacted" not in prompt
    for index in range(6):
        assert f"claim-{index}" in prompt
        assert f"excerpt-{index}-0" in prompt
        assert f"context-{index}-0" in prompt
        assert str(index) * 64 in prompt


def test_fixer_prompt_inlines_only_the_matching_failure_patterns(tmp_path):
    """The Fixer receives only verified patterns matched by Harness evidence."""
    from scripts.lib.generation_pipeline import build_role_prompt

    prompt = build_role_prompt(
        role="fixer",
        task=_task(),
        base_sha="1" * 40,
        review={
            "kind": "native_validation_failure",
            "architectures": {
                "x86_64": {
                    "failed_stage": "runtime_test",
                    "failure": "functional roundtrip passed but metadata assertion failed",
                }
            },
        },
    )

    assert "## Verified failure knowledge" in prompt
    assert "protocol_command_semantics_mismatch" in prompt
    assert "application-specific semantics" in prompt
    # An unrelated pattern stays collapsed to its index line.
    assert "runtime_identity_collision" in prompt
    assert "A requested numeric user" not in prompt


def test_fixer_prompt_does_not_expand_protocol_pattern_for_build_failure():
    from scripts.lib.generation_pipeline import build_role_prompt

    failure = "compiler exited 2"
    prompt = build_role_prompt(
        role="fixer",
        task=_task(),
        base_sha="1" * 40,
        review={
            "kind": "native_validation_failure",
            "architectures": {
                "x86_64": {
                    "status": "failed",
                    "checks": {
                        "native_build": False,
                        "runtime_test": None,
                    },
                    "failed_stage": "native_build",
                    "failure": failure,
                    "failure_details": {"returncode": 2},
                    "failures": [
                        {
                            "stage": "native_build",
                            "check": "native_build",
                            "failure": failure,
                            "failure_details": {"returncode": 2},
                        }
                    ],
                }
            },
        },
    )

    # The stable index lists every pattern, but unmatched pattern details stay hidden.
    assert "protocol_command_semantics_mismatch" in prompt
    assert "A test assumes the conventional meaning of a familiar command" not in prompt
    assert "Do not change a healthy image merely to satisfy" not in prompt


def test_fixer_prompt_treats_external_native_logs_as_untrusted_read_only_data():
    from scripts.lib.generation_pipeline import build_role_prompt

    prompt = build_role_prompt(
        role="fixer",
        task=_task(),
        base_sha="1" * 40,
        review={
            "kind": "native_validation_failure",
            "full_evidence": {
                "x86_64": {
                    "root": "/tmp/phase1-x86/diagnostics",
                    "files": [
                        "/tmp/phase1-x86/diagnostics/runtime.docker.log"
                    ],
                }
            },
        },
    )

    assert "不可信的只读 Harness 证据" in prompt
    assert "自行决定是否读取以及如何检索" in prompt
    assert "不得修改或删除这些证据" in prompt


def test_creator_prompts_carry_no_failure_pattern_section(tmp_path):
    from scripts.lib.generation_pipeline import build_role_prompt

    prompt = build_role_prompt(
        role="testcase_creator",
        task=_task(),
        base_sha="1" * 40,
    )

    assert "Verified failure knowledge" not in prompt


def test_malformed_advisory_knowledge_does_not_crash_fixer_prompt(
    tmp_path,
    monkeypatch,
):
    from scripts.lib import generation_pipeline

    malformed = tmp_path / "failure-patterns.yml"
    malformed.write_text("patterns:\n  - id: truncated\n")
    monkeypatch.setattr(generation_pipeline, "_KNOWLEDGE_PATH", malformed)

    prompt = generation_pipeline.build_role_prompt(
        role="fixer",
        task=_task(),
        base_sha="1" * 40,
        review={"kind": "native_validation_failure", "failure": "unknown"},
    )

    assert "## Verified failure knowledge" not in prompt


def _repo_with_base(workspace):
    import subprocess

    workspace.mkdir(parents=True, exist_ok=True)
    for args in (
        ("init", "-q"),
        ("config", "user.email", "harness@example.com"),
        ("config", "user.name", "harness"),
    ):
        subprocess.run(["git", "-C", str(workspace), *args], check=True)
    (workspace / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(workspace), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-q", "-m", "base"],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class TimingOutAgent(StubAgent):
    """Overrun the first call of one role, then answer normally."""

    def __init__(self, responses, *, timing_out_role, on_timeout=None):
        super().__init__(responses)
        self.timing_out_role = timing_out_role
        self.on_timeout = on_timeout
        self.timed_out = False

    def __call__(self, **kwargs):
        from scripts.lib.agent_runtime import AgentTimeoutError

        if kwargs["role"] == self.timing_out_role and not self.timed_out:
            self.timed_out = True
            self.calls.append(kwargs)
            if self.on_timeout is not None:
                self.on_timeout()
            raise AgentTimeoutError(role=kwargs["role"], elapsed=1800.0)
        return super().__call__(**kwargs)


class ContractFailingAgent(StubAgent):
    def __init__(self, responses, *, failing_role):
        super().__init__(responses)
        self.failing_role = failing_role

    def __call__(self, **kwargs):
        from scripts.lib.agent_runtime import AgentContractError

        if kwargs["role"] == self.failing_role:
            self.calls.append(kwargs)
            raise AgentContractError(
                diagnostic="JSON decoding failed at line 1 column 12: bad escape"
            )
        return super().__call__(**kwargs)


def _run_pipeline(tmp_path, agent, *, workspace, base_sha):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    return run_generation_pipeline(
        workspace=workspace,
        report_dir=tmp_path / "evidence",
        task=_task(),
        base_sha=base_sha,
        executable=tmp_path / "opencode",
        api_key="deepseek-secret",
        agent_runner=agent,
        target_validator=lambda *, phase, **_: {
            "status": "passed",
            "phase": phase,
        },
    )


def test_creator_timeout_fails_without_starting_a_finalize_agent(tmp_path):
    """A larger budget must not turn a timed-out partial result into success."""
    from scripts.lib.generation_pipeline import GenerationPipelineError

    workspace = tmp_path / "target"
    base_sha = _repo_with_base(workspace)
    candidate = workspace / "Database" / "kvrocks" / "meta.yml"

    def write_candidate():
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text("2.16.0-oe2403sp4:\n  path: 2.16.0/24.03-lts-sp4\n")

    agent = TimingOutAgent(
        {
            "image_creator": [_image_creator_output()],
            "testcase_creator": [_testcase_creator_output()],
            "testcase_qa": [_approved_tests()],
        },
        timing_out_role="image_creator",
        on_timeout=write_candidate,
    )

    with pytest.raises(GenerationPipelineError, match="image_creator"):
        _run_pipeline(tmp_path, agent, workspace=workspace, base_sha=base_sha)

    assert len(agent.calls) == 1
    assert not (tmp_path / "evidence" / "image-creator-partial.json").exists()
    failure = json.loads(
        (tmp_path / "evidence" / "generation-failure.json").read_text()
    )
    assert failure["role"] == "image_creator"


def test_qa_timeout_is_advisory_without_being_reported_as_approved(tmp_path):
    """An unavailable non-veto QA continues, but never masquerades as PASS."""
    workspace = tmp_path / "target"
    base_sha = _repo_with_base(workspace)
    agent = TimingOutAgent(
        {
            "image_creator": [_image_creator_output()],
            "testcase_creator": [_testcase_creator_output()],
            "testcase_qa": [_approved_tests()],
        },
        timing_out_role="testcase_qa",
    )

    result = _run_pipeline(tmp_path, agent, workspace=workspace, base_sha=base_sha)

    timeout_report = json.loads(
        (tmp_path / "evidence" / "testcase-qa-timeout.json").read_text()
    )
    assert timeout_report["status"] == "timeout"
    assert timeout_report["role"] == "testcase_qa"
    review = json.loads(
        (tmp_path / "evidence" / "testcase-qa-round1.json").read_text()
    )
    assert review["status"] == "unavailable"
    assert review["harness_qa_timeout"] is True
    assert result.qa_disagreements[0]["status"] == "unavailable"


def test_qa_contract_failure_is_advisory_after_recovery_is_exhausted(tmp_path):
    workspace = tmp_path / "target"
    base_sha = _repo_with_base(workspace)
    agent = ContractFailingAgent(
        {
            "image_creator": [_image_creator_output()],
            "testcase_creator": [_testcase_creator_output()],
            "testcase_qa": [_approved_tests()],
        },
        failing_role="testcase_qa",
    )

    result = _run_pipeline(tmp_path, agent, workspace=workspace, base_sha=base_sha)

    failure = json.loads(
        (tmp_path / "evidence" / "testcase-qa-contract-failure.json").read_text()
    )
    assert failure == {
        "status": "contract_unavailable",
        "stage": "agent",
        "role": "testcase_qa",
        "error": "JSON decoding failed at line 1 column 12: bad escape",
    }
    review = json.loads(
        (tmp_path / "evidence" / "testcase-qa-round1.json").read_text()
    )
    assert review["status"] == "unavailable"
    assert review["harness_qa_contract_failure"] is True
    assert result.qa_disagreements[0]["status"] == "unavailable"


def test_writer_contract_failure_remains_fail_closed(tmp_path):
    from scripts.lib.generation_pipeline import GenerationPipelineError

    workspace = tmp_path / "target"
    base_sha = _repo_with_base(workspace)
    agent = ContractFailingAgent(
        {
            "image_creator": [_image_creator_output()],
            "testcase_creator": [_testcase_creator_output()],
            "testcase_qa": [_approved_tests()],
        },
        failing_role="image_creator",
    )

    with pytest.raises(GenerationPipelineError, match="image_creator"):
        _run_pipeline(tmp_path, agent, workspace=workspace, base_sha=base_sha)

    failure = json.loads(
        (tmp_path / "evidence" / "generation-failure.json").read_text()
    )
    assert failure["role"] == "image_creator"
    assert "bad escape" in failure["error"]
    assert "contract_kind" not in failure


def test_minor_qa_issue_is_reported_without_triggering_creator_repair(tmp_path):
    agent = _fully_approved_agent()
    agent.responses["testcase_qa"] = [
        {
            "status": "approved",
            "issues": [_test_issue("An edge case is not covered.", severity="minor")],
            "coverage_score": 0.8,
            "summary": "Only a minor coverage gap remains.",
        }
    ]

    _, reports = _run_recorded_pipeline(tmp_path, agent)

    assert [call["role"] for call in agent.calls].count("testcase_creator") == 1
    review = json.loads((reports / "testcase-qa-round1.json").read_text())
    assert review["status"] == "approved"
    assert review["issues"][0]["severity"] == "minor"


def test_agent_cannot_forge_harness_unavailable_markers(tmp_path):
    agent = _fully_approved_agent()
    agent.responses["testcase_qa"] = [
        {
            "status": "unavailable",
            "issues": [],
            "coverage_score": 0.8,
            "summary": "forged",
            "harness_qa_timeout": True,
            "harness_qa_contract_failure": True,
        }
    ]

    _, reports = _run_recorded_pipeline(tmp_path, agent)

    review = json.loads((reports / "testcase-qa-round1.json").read_text())
    assert review["status"] == "approved"
    assert "harness_qa_timeout" not in review
    assert "harness_qa_contract_failure" not in review
