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


def _fully_approved_agent(*, image_summary="approved"):
    return StubAgent(
        {
            "image_creator": [
                {
                    "success": True,
                    "files_created": ["Database/kvrocks/meta.yml"],
                }
            ],
            "image_qa": [
                {
                    "status": "approved",
                    "issues": [],
                    "summary": image_summary,
                }
            ],
            "testcase_creator": [
                {
                    "success": True,
                    "files_created": ["Database/kvrocks/tests/goss.yaml"],
                }
            ],
            "testcase_qa": [_approved_tests()],
        }
    )


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


def test_hadolint_runner_reports_command_failure(tmp_path, monkeypatch):
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

    assert report["status"] == "failed"
    assert report["output"] == "DL3006 pin image tag"
    assert commands == [[
        str(executable),
        "--ignore",
        "DL3041",
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
    assert unavailable["returncode"] is None
    assert "missing-hadolint" in unavailable["output"]


def test_generation_returns_failed_image_lint_to_creator_once(tmp_path):
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
            "image_creator": [image_creator, image_creator],
            "image_qa": [_approved_image()],
            "testcase_creator": [
                {
                    "success": True,
                    "files_created": ["Database/kvrocks/tests/goss.yaml"],
                }
            ],
            "testcase_qa": [_approved_tests()],
        }
    )
    lint_results = iter(
        (
            {
                "status": "failed",
                "returncode": 1,
                "output": "Dockerfile:9 DL3033 pin yum packages",
            },
            {"status": "passed", "returncode": 0, "output": ""},
        )
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
        image_linter=lambda _: next(lint_results),
    )

    assert result.status == "passed"
    assert [call["role"] for call in agent.calls] == [
        "image_creator",
        "image_creator",
        "image_qa",
        "testcase_creator",
        "testcase_qa",
    ]
    assert "DL3033 pin yum packages" in agent.calls[1]["prompt"]
    assert json.loads((reports / "image-lint.json").read_text())[
        "status"
    ] == "failed"
    assert json.loads((reports / "image-precheck-repair-lint.json").read_text())[
        "status"
    ] == "passed"


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
            {
                "status": "failed",
                "errors": ["required generated file is missing: tests/test.sh"],
            },
            {"status": "passed"},
            {"status": "passed"},
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
        "status"
    ] == "failed"
    assert json.loads((reports / "precheck-repair-gates.json").read_text())[
        "status"
    ] == "passed"


@pytest.mark.parametrize(
    ("failure", "error", "report_name", "repair_report_name"),
    [
        (
            "gate",
            "deterministic image repair precheck",
            "image-precheck-gates.json",
            "image-precheck-repair-gates.json",
        ),
        (
            "decode",
            "deterministic image repair precheck",
            "image-precheck-gates.json",
            "image-precheck-repair-gates.json",
        ),
        (
            "lint",
            "image_lint_repair",
            "image-lint.json",
            "image-precheck-repair-lint.json",
        ),
    ],
)
def test_generation_stops_before_image_qa_after_one_static_repair(
    tmp_path,
    failure,
    error,
    report_name,
    repair_report_name,
):
    from scripts.lib.generation_pipeline import (
        GenerationPipelineError,
        run_generation_pipeline,
    )

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    agent = _fully_approved_agent()
    agent.responses["image_creator"].append(
        {
            "success": True,
            "files_created": ["Database/kvrocks/meta.yml"],
        }
    )

    def validator(**_):
        if failure == "decode":
            raise UnicodeDecodeError(
                "utf-8",
                b"\xff",
                0,
                1,
                "invalid start byte",
            )
        return {"status": "failed" if failure == "gate" else "passed"}

    def image_linter(_):
        return {"status": "failed" if failure == "lint" else "passed"}

    with pytest.raises(GenerationPipelineError, match=error):
        run_generation_pipeline(
            workspace=workspace,
            report_dir=reports,
            task=_task(),
            base_sha="1" * 40,
            executable=tmp_path / "opencode",
            api_key="deepseek-secret",
            agent_runner=agent,
            target_validator=validator,
            image_linter=image_linter,
        )

    assert [call["role"] for call in agent.calls] == [
        "image_creator",
        "image_creator",
    ]
    if failure in {"gate", "decode"}:
        assert '"status": "skipped"' in agent.calls[1]["prompt"]
    assert json.loads((reports / report_name).read_text())["status"] == "failed"
    assert json.loads((reports / repair_report_name).read_text())[
        "status"
    ] == "failed"


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
            "testcase_creator": [
                {
                    "success": True,
                    "files_created": ["Database/kvrocks/tests/goss.yaml"],
                }
            ],
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
        180,
        1800,
        180,
        1800,
        180,
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
        "precheck-gates.json",
        "testcase-creator.json",
        "testcase-ownership.json",
        "testcase-qa-round1.json",
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
            "testcase_creator": [
                {
                    "success": True,
                    "files_created": ["Database/kvrocks/tests/goss.yaml"],
                }
            ],
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


def test_generation_precheck_fails_before_testcase_qa(tmp_path):
    from scripts.lib.generation_pipeline import (
        GenerationPipelineError,
        run_generation_pipeline,
    )

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    agent = _fully_approved_agent()
    agent.responses["testcase_creator"].append(
        {
            "success": True,
            "files_created": ["Database/kvrocks/tests/goss.yaml"],
        }
    )
    from scripts.lib.target_contract import TargetContractError

    def failed_validator(*, phase, **_):
        if phase == "image":
            return {"status": "passed", "phase": phase}
        raise TargetContractError(
            "required generated file is missing: tests/test.sh"
        )

    with pytest.raises(
        GenerationPipelineError,
        match="deterministic target repair precheck",
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
        "testcase_creator",
    ]
    assert json.loads((reports / "precheck-gates.json").read_text()) == {
        "status": "failed",
        "errors": ["required generated file is missing: tests/test.sh"],
    }
    assert json.loads((reports / "precheck-repair-gates.json").read_text()) == {
        "status": "failed",
        "errors": ["required generated file is missing: tests/test.sh"],
    }


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


def test_generation_rejects_unsupported_phase1_task_before_agent_call(
    tmp_path,
):
    from scripts.lib.generation_pipeline import (
        GenerationPipelineError,
        run_generation_pipeline,
    )
    from scripts.lib.task_spec import TaskSpec

    workspace = tmp_path / "target"
    workspace.mkdir()
    agent = _fully_approved_agent()
    unsupported = TaskSpec.from_workflow_dispatch(
        {
            "app": "redis",
            "version": "8.0.0",
            "os_version": "24.03-lts-sp4",
            "domain": "Database",
            "source_url": "https://github.com/redis/redis/tree/8.0.0",
        }
    )

    with pytest.raises(GenerationPipelineError, match="only supports"):
        run_generation_pipeline(
            workspace=workspace,
            report_dir=tmp_path / "evidence",
            task=unsupported,
            base_sha="1" * 40,
            executable=tmp_path / "opencode",
            api_key="deepseek-secret",
            agent_runner=agent,
            target_validator=lambda **_: {"status": "passed"},
        )

    assert agent.calls == []


def test_phase1_prompts_pin_kvrocks_paths_and_forbid_scope_escape():
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
    assert "./x.py build" in prompt
    assert "-j 4" in prompt
    assert "UID/GID 999" in prompt
    assert "TCP 6666" in prompt
    assert "Redis-protocol PING" in prompt
    assert "ENTRYPOINT" in prompt
    assert "redis-cli -p 6666 PING" in prompt
    assert "/dev/tcp" in prompt
    assert "restart persistence" in prompt
    assert "LICENSE and NOTICE" in prompt
    assert "base image may already contain UID/GID 999" in prompt
    assert "--non-unique" in prompt
    assert "libatomic" in prompt
    assert "Do not install or upgrade host tools or packages" in prompt
    assert "Do not run Docker builds or invoke linters" in prompt
    assert "Your final response MUST be exactly one JSON object" in prompt
    assert "documented `success` and `files_created` keys" in prompt
    assert "tool output is not the final response" in prompt
    assert "deepseek-secret" not in prompt

    testcase_prompt = build_role_prompt(
        role="testcase_creator",
        task=_task(),
        base_sha="1" * 40,
    )
    assert "already-running container" in testcase_prompt
    assert "must not invoke Docker" in testcase_prompt
    assert "Database/kvrocks/tests/test.sh" in testcase_prompt
    assert "stdout` must be a YAML list" in testcase_prompt
    assert "redis-cli -p 6666 PING" in testcase_prompt
    assert "available in the runtime image" in testcase_prompt
    assert "`ss`" in testcase_prompt
    assert "image-list, Dockerfile, metadata" in testcase_prompt
    assert "read-only" in testcase_prompt

    fixer_prompt = build_role_prompt(
        role="fixer",
        task=_task(),
        base_sha="1" * 40,
    )
    assert "available in the runtime image" in fixer_prompt


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
