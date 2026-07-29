from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_existing_harness_modules_keep_shared_public_apis():
    from scripts.harness import compose_pr, gate_diff
    from scripts.utils import artifacts, gitcode

    assert callable(gate_diff.validate_generated_target)
    assert callable(gate_diff.validate_final_target)

    assert callable(compose_pr.compose_pull_request)
    assert callable(compose_pr.deliver_promoted_candidate)

    assert callable(gitcode.GitCodeClient)
    assert callable(artifacts.aggregate_native_results)


def test_phase_one_has_one_public_cli_without_legacy_delegation():
    flow = (ROOT / "scripts" / "harness" / "flow.py").read_text()
    run = (ROOT / "scripts" / "harness" / "run.py").read_text()

    assert not (ROOT / "scripts" / "harness" / "phase1.py").exists()
    assert "from scripts.harness.run import" not in flow
    assert "phase1-generate" in flow
    assert "phase1-generate" not in run


def test_agent_execution_is_exposed_through_flow_orchestrator():
    workflow = (
        ROOT / ".github" / "workflows" / "new-image.yml"
    ).read_text()

    assert "scripts/harness/flow.py" in workflow
    assert "scripts/harness/run.py" not in workflow
    assert "scripts/harness/phase1.py" not in workflow
    assert "agent-generate" not in workflow
    assert "native-validate-repair" not in workflow


def test_workflow_uses_existing_validation_and_artifact_clis():
    workflow = (
        ROOT / ".github" / "workflows" / "new-image.yml"
    ).read_text()
    flow = (ROOT / "scripts" / "harness" / "flow.py").read_text()

    assert "scripts/harness/gate_diff.py" in workflow
    assert "scripts/utils/artifacts.py" in workflow
    assert "phase1-native-validate" in workflow
    assert 'add_parser("target-validate")' not in flow
    assert 'add_parser("results-aggregate")' not in flow
    assert 'add_parser("native-validate")' not in flow
