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
        from scripts.harness.run import AgentResult

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


def test_generation_runs_adversarial_pairs_and_one_target_gate(
    tmp_path,
    capsys,
):
    from scripts.harness.run import run_generation_pipeline

    workspace = tmp_path / "target"
    reports = tmp_path / "evidence"
    workspace.mkdir()
    agent = StubAgent(
        {
            "image_creator": [
                {
                    "success": True,
                    "files_created": ["Database/kvrocks/meta.yml"],
                }
            ],
            "image_qa": [
                {
                    "status": "needs_fix",
                    "issues": [{"severity": "major", "description": "fix health"}],
                    "summary": "one issue",
                },
                _approved_image(),
            ],
            "fixer": [{"success": True, "changes": ["health check"]}],
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
        "fixer",
        "image_qa",
        "testcase_creator",
        "testcase_qa",
    ]
    assert len(gate_calls) == 1
    assert gate_calls[0]["workspace"] == workspace
    assert sorted(path.name for path in reports.iterdir()) == [
        "fixer-image-round1.json",
        "gates.json",
        "image-creator.json",
        "image-qa-round1.json",
        "image-qa-round2.json",
        "testcase-creator.json",
        "testcase-qa-round1.json",
    ]
    gate_report = json.loads((reports / "gates.json").read_text())
    assert gate_report["status"] == "passed"
    assert "deepseek-secret" not in json.dumps(
        [json.loads(path.read_text()) for path in reports.iterdir()]
    )
    output = capsys.readouterr().out
    markers = [
        "[flow][generate] START image_creator",
        "[flow][generate] PASS image_creator",
        "[flow][review] START image_qa round=1",
        "[flow][repair] START fixer subject=image",
        "[flow][review] PASS image_qa round=2",
        "[flow][generate] START testcase_creator",
        "[flow][review] PASS testcase_qa round=1",
        "[flow][gate] PASS target_contract",
    ]
    positions = [output.index(marker) for marker in markers]
    assert positions == sorted(positions)


def test_generation_fails_closed_when_second_qa_still_requests_fix(tmp_path):
    from scripts.harness.run import (
        GenerationPipelineError,
        run_generation_pipeline,
    )

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
                {"success": True, "files_created": ["Database/kvrocks/meta.yml"]}
            ],
            "image_qa": [needs_fix, needs_fix],
            "fixer": [{"success": True, "changes": ["attempted fix"]}],
        }
    )

    with pytest.raises(GenerationPipelineError, match="image_qa"):
        run_generation_pipeline(
            workspace=workspace,
            report_dir=reports,
            task=_task(),
            base_sha="1" * 40,
            executable=tmp_path / "opencode",
            api_key="deepseek-secret",
            agent_runner=agent,
            target_validator=lambda **_: pytest.fail("gate must not run"),
        )


def test_generation_reports_must_be_outside_target_workspace(tmp_path):
    from scripts.harness.run import (
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


def test_phase1_prompts_pin_kvrocks_paths_and_forbid_scope_escape():
    from scripts.harness.run import build_role_prompt

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
