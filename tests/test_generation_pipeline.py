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


def test_generation_runs_adversarial_pairs_and_one_target_gate(
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
    assert "QA report to resolve" in agent.calls[2]["prompt"]
    assert "fix health" in agent.calls[2]["prompt"]
    assert "Only fix the reported QA issues" in agent.calls[2]["prompt"]
    assert (
        "Your final response MUST be exactly one JSON object"
        in agent.calls[2]["prompt"]
    )
    assert len(gate_calls) == 2
    assert gate_calls[0]["workspace"] == workspace
    assert sorted(path.name for path in reports.iterdir()) == [
        "gates.json",
        "image-creator-round2.json",
        "image-creator.json",
        "image-qa-round1.json",
        "image-qa-round2.json",
        "precheck-gates.json",
        "testcase-creator.json",
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
    assert len(gate_calls) == 2
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
    gate_results = iter(({"status": "passed"}, failed_gate))

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
    from scripts.lib.target_contract import TargetContractError

    def failed_validator(**_):
        raise TargetContractError(
            "required generated file is missing: tests/test.sh"
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
    (image / "test.sh").write_text("exec ../../tests/test.sh\n")
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
    assert "Database/kvrocks/tests/test.sh" in testcase_qa_prompt
    assert "redis-cli -p 6666 PING" in testcase_qa_prompt


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
    assert "restart persistence" in prompt
    assert "LICENSE and NOTICE" in prompt
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
