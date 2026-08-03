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


def _approved_image():
    return {"status": "approved", "issues": [], "summary": "approved"}


def _approved_tests():
    return {
        "status": "approved",
        "issues": [],
        "coverage_score": 0.95,
        "summary": "approved",
    }


def _testcase_creator_output(**overrides):
    payload = {
        "success": True,
        "files_created": ["Database/kvrocks/tests/goss.yaml"],
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


def _fully_approved_agent(*, image_summary="approved"):
    return StubAgent(
        {
            "image_creator": [
                {
                    "success": True,
                    "files_created": ["Database/kvrocks/meta.yml"],
                    "identity_decision": {
                        "mode": "dynamic",
                        "user": "kvrocks",
                        "group": "kvrocks",
                        "uid": None,
                        "gid": None,
                        "requirement_evidence_ids": [],
                    },
                    "evidence": [],
                }
            ],
            "image_qa": [
                {
                    "status": "approved",
                    "issues": [],
                    "summary": image_summary,
                }
            ],
            "testcase_creator": [_testcase_creator_output()],
            "testcase_qa": [_approved_tests()],
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


def _agent_with_repair(pair):
    image_creator = {
        "success": True,
        "files_created": ["Database/kvrocks/meta.yml"],
    }
    testcase_creator = {
        "success": True,
        "files_created": ["Database/kvrocks/tests/goss.yaml"],
    }
    image_qa = [_approved_image()]
    testcase_qa = [_approved_tests()]
    if pair == "image":
        image_qa = [
            {
                "status": "needs_fix",
                "issues": [{"severity": "major", "description": "fix health"}],
                "summary": "one issue",
            },
            _approved_image(),
        ]
    else:
        testcase_qa = [
            {
                "status": "needs_fix",
                "issues": [
                    {"severity": "major", "description": "fix version check"}
                ],
                "coverage_score": 0.70,
                "summary": "one issue",
            },
            _approved_tests(),
        ]
    return StubAgent(
        {
            "image_creator": [image_creator] * (2 if pair == "image" else 1),
            "image_qa": image_qa,
            "testcase_creator": [testcase_creator]
            * (2 if pair == "testcase" else 1),
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


def test_generation_lints_image_before_paid_qa(tmp_path):
    events, reports = _run_recorded_pipeline(
        tmp_path,
        _fully_approved_agent(),
        lint=True,
    )

    assert events == [
        "agent:image_creator",
        "gate:image",
        "lint:Dockerfile",
        "agent:image_qa",
        "agent:testcase_creator",
        "gate:full",
        "agent:testcase_qa",
        "gate:full",
    ]
    assert json.loads((reports / "image-lint.json").read_text()) == {
        "status": "passed"
    }


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
    image_creator = {
        "success": True,
        "files_created": ["Database/kvrocks/meta.yml"],
    }
    agent = StubAgent(
        {
            "image_creator": [image_creator],
            "image_qa": [_approved_image()],
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
        "image_qa",
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
        "files_created": ["Database/kvrocks/tests/goss.yaml"],
    }
    agent = StubAgent(
        {
            "image_creator": [
                {
                    "success": True,
                    "files_created": ["Database/kvrocks/meta.yml"],
                }
            ],
            "image_qa": [_approved_image()],
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
        "image_qa",
        "testcase_creator",
        "testcase_creator",
        "testcase_qa",
    ]
    assert "required generated file is missing" in agent.calls[3]["prompt"]
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
    image_creator = {
        "success": True,
        "files_created": ["Database/kvrocks/README.md"],
    }
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
        "image_qa",
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
        "files_created": ["Database/kvrocks/tests/goss.yaml"],
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
            code="tests.goss_yaml",
            message="goss.yaml must be valid YAML",
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
        _agent_with_repair("testcase")
        if repair
        else _fully_approved_agent()
    )
    testcase_calls = 0

    def mutate(role, _):
        nonlocal testcase_calls
        if role == "image_creator":
            dockerfile.parent.mkdir(parents=True)
            dockerfile.write_text("FROM scratch\n")
        elif role == "testcase_creator":
            testcase_calls += 1
            if testcase_calls == (2 if repair else 1):
                dockerfile.write_text("FROM scratch\nRUN echo changed\n")

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


@pytest.mark.parametrize("pair", ["image", "testcase"])
def test_generation_rechecks_creator_repair_before_second_qa(
    tmp_path,
    pair,
):
    events, _ = _run_recorded_pipeline(
        tmp_path,
        _agent_with_repair(pair),
        lint=True,
    )

    if pair == "image":
        assert events[:8] == [
            "agent:image_creator",
            "gate:image",
            "lint:Dockerfile",
            "agent:image_qa",
            "agent:image_creator",
            "gate:image",
            "lint:Dockerfile",
            "agent:image_qa",
        ]
    else:
        assert events[-7:-1] == [
            "agent:testcase_creator",
            "gate:full",
            "agent:testcase_qa",
            "agent:testcase_creator",
            "gate:full",
            "agent:testcase_qa",
        ]


def test_generation_runs_adversarial_pairs_and_records_evidence(
    tmp_path,
    capsys,
):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    agent = StubAgent(
        {
            "image_creator": [
                {
                    "success": True,
                    "files_created": ["Database/kvrocks/meta.yml"],
                },
                {
                    "success": True,
                    "files_created": ["Database/kvrocks/meta.yml"],
                },
            ],
            "image_qa": [
                {
                    "status": "needs_fix",
                    "issues": [{"severity": "major", "description": "fix health"}],
                    "summary": "one issue",
                },
                _approved_image(),
            ],
            "testcase_creator": [_testcase_creator_output()],
            "testcase_qa": [_approved_tests()],
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
        "image_qa",
        "image_creator",
        "image_qa",
        "testcase_creator",
        "testcase_qa",
    ]
    assert [call["timeout"] for call in agent.calls] == [
        1800,
        900,
        1800,
        900,
        1800,
        900,
    ]
    assert "Review report to resolve" in agent.calls[2]["prompt"]
    assert "fix health" in agent.calls[2]["prompt"]
    assert "Only fix the reported issues" in agent.calls[2]["prompt"]
    assert (
        "Your final response MUST be exactly one JSON object"
        in agent.calls[2]["prompt"]
    )
    assert "Previous QA findings to verify" not in agent.calls[1]["prompt"]
    assert "Previous QA findings to verify" in agent.calls[3]["prompt"]
    assert "fix health" in agent.calls[3]["prompt"]
    assert "independent QA session" in agent.calls[3]["prompt"]
    assert "complete review" in agent.calls[3]["prompt"]
    assert [call["phase"] for call in gate_calls] == [
        "image",
        "image",
        "full",
        "full",
    ]
    assert gate_calls[0]["workspace"] == workspace
    assert sorted(path.name for path in reports.iterdir()) == [
        "gates.json",
        "image-creator-round2.json",
        "image-creator.json",
        "image-precheck-gates.json",
        "image-qa-round1.json",
        "image-qa-round2.json",
        "image-repair-gates.json",
        "image-round1-evidence-bundle.json",
        "image-round2-evidence-bundle.json",
        "precheck-gates.json",
        "testcase-creator.json",
        "testcase-ownership.json",
        "testcase-qa-round1.json",
        "testcase-round1-evidence-bundle.json",
    ]
    gate_report = json.loads((reports / "gates.json").read_text())
    assert gate_report["status"] == "passed"
    assert "deepseek-secret" not in json.dumps(
        [json.loads(path.read_text()) for path in reports.iterdir()]
    )
    output = capsys.readouterr().out
    assert (
        '[flow][review] RESULT image_qa round=1 status=needs_fix '
        'issues=1 summary="one issue"'
    ) in output
    assert (
        '[flow][review] RESULT image_qa round=2 status=approved '
        'issues=0 summary="approved"'
    ) in output
    markers = [
        "[flow][generate] START image_creator",
        "[flow][generate] PASS image_creator",
        "[flow][review] START image_qa round=1",
        "[flow][repair] START image_creator round=2",
        "[flow][review] PASS image_qa round=2",
        "[flow][generate] START testcase_creator",
        "[flow][gate] PASS generated_precheck",
        "[flow][review] PASS testcase_qa round=1",
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
        "issues": [{"severity": "blocker", "description": "still broken"}],
        "summary": "not approved",
    }
    agent = StubAgent(
        {
            "image_creator": [
                {"success": True, "files_created": ["Database/kvrocks/meta.yml"]},
                {"success": True, "files_created": ["Database/kvrocks/meta.yml"]},
            ],
            "image_qa": [needs_fix, needs_fix],
            "testcase_creator": [_testcase_creator_output()],
            "testcase_qa": [_approved_tests()],
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
            "role": "image_qa",
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
        "image",
        "full",
        "full",
    ]
    assert json.loads((reports / "image-qa-round2.json").read_text())[
        "status"
    ] == "needs_fix"
    assert (
        "[flow][review] DISAGREEMENT image_qa round=2; "
        "continue=local_validation"
    ) in capsys.readouterr().out


def test_image_qa_receives_latest_identity_decision_after_precheck_repair(
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
            "requirement_evidence_ids": [],
        },
    }
    repaired = {
        "success": True,
        "files_created": ["Database/kvrocks/meta.yml"],
        "summary": "deterministic repair applied",
        "identity_decision": {
            "mode": "fixed",
            "user": "kvrocks",
            "group": "kvrocks",
            "uid": 991,
            "gid": 991,
            "requirement_evidence_ids": ["upstream-identity-001"],
        },
    }
    agent = StubAgent(
        {
            "image_creator": [initial, repaired],
            "image_qa": [_approved_image()],
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

    image_qa_prompt = next(
        call["prompt"] for call in agent.calls if call["role"] == "image_qa"
    )
    assert "Image Creator identity decision" in image_qa_prompt
    assert '"uid": 991' in image_qa_prompt
    assert "upstream-identity-001" in image_qa_prompt
    assert '"files_created"' in image_qa_prompt
    assert "deterministic repair applied" in image_qa_prompt


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
            "image_creator": [
                {
                    "success": True,
                    "files_created": ["Database/kvrocks/meta.yml"],
                }
            ],
            "image_qa": [_approved_image()],
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
        image_summary="approved\nwithout deepseek-secret",
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
        "image_qa",
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


def test_qa_prompts_embed_candidate_snapshot_without_tool_reads(tmp_path):
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
    (image / "Dockerfile").write_text("FROM openEuler\n")
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

    image_qa_prompt = agent.calls[1]["prompt"]
    testcase_qa_prompt = agent.calls[3]["prompt"]
    assert "Embedded candidate snapshot" in image_qa_prompt
    assert "Do not call tools" in image_qa_prompt
    assert "Database/kvrocks/2.16.0/24.03-lts-sp4/Dockerfile" in image_qa_prompt
    assert "FROM openEuler" in image_qa_prompt
    assert "Database/kvrocks/tests/test.sh" not in image_qa_prompt
    assert "Required shared test files" not in image_qa_prompt
    assert "Embedded candidate snapshot" in testcase_qa_prompt
    assert "Database/kvrocks/2.16.0/24.03-lts-sp4/test.sh" not in (
        testcase_qa_prompt
    )
    assert "Database/kvrocks/tests/test.sh" in testcase_qa_prompt
    assert "redis-cli -p 6666 PING" in testcase_qa_prompt


def test_image_qa_snapshot_includes_auxiliary_image_files(tmp_path):
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
            "FROM openEuler\nCOPY service.conf /etc/example/service.conf\n"
        )
        (auxiliary_root / "service.conf").write_text("listen = 0.0.0.0\n")
        (auxiliary_root / "entrypoint.sh").write_text("#!/bin/sh\nexec example\n")
        tests_root = auxiliary_root.parents[2] / "tests"
        tests_root.mkdir()
        (tests_root / "test.sh").write_text("should not reach image QA\n")
        results_root = auxiliary_root.parents[2] / "results"
        results_root.mkdir()
        (results_root / "results.json").write_text('{"status": "passed"}\n')

    _run_recorded_pipeline(
        tmp_path,
        agent,
        mutation=write_candidate,
    )

    image_qa_prompt = agent.calls[1]["prompt"]
    assert (
        "Database/kvrocks/2.16.0/24.03-lts-sp4/service.conf"
        in image_qa_prompt
    )
    assert "listen = 0.0.0.0" in image_qa_prompt
    assert (
        "Database/kvrocks/2.16.0/24.03-lts-sp4/entrypoint.sh"
        in image_qa_prompt
    )
    assert "Database/kvrocks/tests/test.sh" not in image_qa_prompt
    assert "Database/kvrocks/results/results.json" not in image_qa_prompt


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


def test_qa_snapshot_failure_writes_machine_readable_report(tmp_path):
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
            dockerfile.write_text("X" * 70_000)

    from scripts.lib.generation_pipeline import GenerationPipelineError
    with pytest.raises(
        GenerationPipelineError,
        match="candidate snapshot is too large",
    ):
        _run_recorded_pipeline(
            tmp_path,
            agent,
            mutation=write_oversized_candidate,
        )

    assert [call["role"] for call in agent.calls] == ["image_creator"]
    report = tmp_path / "evidence" / "generation-failure.json"
    assert json.loads(report.read_text()) == {
        "status": "failed",
        "stage": "qa_snapshot",
        "role": "image_qa",
        "error": "candidate snapshot is too large for bounded QA",
    }


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


def test_unavailable_creator_evidence_reaches_qa_without_a_repair_round(tmp_path):
    from scripts.lib.generation_pipeline import run_generation_pipeline

    workspace = tmp_path / "target"
    workspace.mkdir()
    agent = _fully_approved_agent()
    image_payload = agent.responses["image_creator"][0]
    image_payload["evidence"] = [
        {
            "id": "identity-runtime-001",
            "claim": "upstream runs as uid 999",
            "source": (
                "https://github.com/apache/kvrocks/blob/"
                "v2.16.0/Dockerfile"
            ),
            "excerpts": ["USER 999"],
        }
    ]
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
    assert resolver_calls[0]["evidence"] == image_payload["evidence"]
    image_qa_call = next(call for call in agent.calls if call["role"] == "image_qa")
    assert '"status": "unavailable"' in image_qa_call["prompt"]
    assert [call["role"] for call in agent.calls].count("image_creator") == 1


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
            files_created=["Cloud/caddy/tests/goss.yaml"],
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
    assert "Do not run Docker builds or invoke linters" in prompt
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
    assert "service mode" in testcase_prompt
    assert "already-running container" in testcase_prompt
    assert "CLI/one-shot mode" in testcase_prompt
    assert "container entrypoint" in testcase_prompt
    assert "must not invoke Docker" in testcase_prompt
    assert "Database/kvrocks/tests/test.sh" in testcase_prompt
    assert "available in the runtime image" in testcase_prompt
    assert "`ss`" in testcase_prompt
    assert "image-list, Dockerfile, metadata" in testcase_prompt
    assert "read-only" in testcase_prompt
    assert "order-independent" in testcase_prompt
    assert "stateful sequence" in testcase_prompt
    assert "final Dockerfile" in testcase_prompt
    for fragment in ("redis-cli", "6666", "UID 999", "kvrocks --version"):
        assert fragment not in testcase_prompt

    fixer_prompt = build_role_prompt(
        role="fixer",
        task=_task(),
        base_sha="1" * 40,
    )
    assert "available in the runtime image" in fixer_prompt
    assert "observable runtime contract" in fixer_prompt
    assert "dependent candidate files" in fixer_prompt


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
        assert "available in the runtime image" in prompt
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
    assert "Database/kvrocks/tests/goss.yaml" in prompt
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
    created = {
        "success": True,
        "files_created": ["Database/kvrocks/meta.yml"],
    }
    agent = StubAgent(
        {
            "image_creator": [created],
            "image_qa": [_approved_image()],
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

    image_qa_prompt = agent.calls[1]["prompt"]
    testcase_qa_prompt = agent.calls[3]["prompt"]
    assert "Testcase Creator command evidence" in testcase_qa_prompt
    assert "refreshed by DBSIZE SCAN" in testcase_qa_prompt
    assert "Harness-fixed Creator evidence bundle" in testcase_qa_prompt
    assert "DBSize::Execute reads the cached key count" in testcase_qa_prompt
    assert "Record an actual candidate concern" not in testcase_qa_prompt
    # Image QA reviews image-owned content and never sees the test evidence.
    assert "Testcase Creator command evidence" not in image_qa_prompt


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
    agent.responses["image_creator"].append(
        dict(agent.responses["image_creator"][0])
    )
    agent.responses["image_qa"] = [
        {
            "status": "needs_fix",
            "issues": [],
            "evidence_reviews": [
                {
                    "evidence_id": "identity-001",
                    "status": "unavailable",
                    "reason": "source could not be fetched",
                }
            ],
            "summary": "candidate is sound but evidence is unavailable",
        },
        _approved_image(),
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
    assert roles.count("image_creator") == 1
    assert roles.count("image_qa") == 1


def test_large_evidence_remains_reviewable_in_the_bounded_qa_prompt(tmp_path):
    from scripts.lib.generation_pipeline import _qa_prompt

    workspace = tmp_path / "target"
    tests = workspace / "Database" / "kvrocks" / "tests"
    tests.mkdir(parents=True)
    (tests / "goss.yaml").write_text("#" + "c" * 59_000)

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
            "files_created": ["Database/kvrocks/tests/goss.yaml"],
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
                    "failed_stage": "dgoss",
                    "failure": (
                        "Error: invalid Attribute for "
                        "File:/var/lib/kvrocks: dir"
                    ),
                }
            },
        },
    )

    assert "## Verified failure knowledge" in prompt
    assert "goss_schema_mismatch" in prompt
    assert "pinned Goss resource schema" in prompt
    # An unrelated pattern stays collapsed to its index line.
    assert "runtime_identity_collision" in prompt
    assert "A requested numeric user" not in prompt


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
