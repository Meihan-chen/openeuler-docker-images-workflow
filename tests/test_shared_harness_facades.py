from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_existing_harness_modules_own_scenario_one_public_apis():
    from scripts.harness import compose_pr, gate_diff, run
    from scripts.utils import artifacts, gitcode

    assert callable(run.run_agent)
    assert callable(run.run_generation_pipeline)
    assert callable(run.validate_native_with_repairs)

    assert callable(gate_diff.validate_generated_target)
    assert callable(gate_diff.validate_final_target)

    assert callable(compose_pr.compose_pull_request)
    assert callable(compose_pr.deliver_promoted_candidate)

    assert callable(gitcode.GitCodeClient)
    assert callable(artifacts.aggregate_native_results)


def test_phase1_orchestration_imports_existing_public_facades():
    phase1 = (ROOT / "scripts" / "harness" / "phase1.py").read_text()
    fork_delivery = (
        ROOT / "scripts" / "lib" / "fork_pr_pipeline.py"
    ).read_text()
    issue_lifecycle = (
        ROOT / "scripts" / "lib" / "issue_lifecycle.py"
    ).read_text()

    assert "scripts.lib.generation_pipeline" not in phase1
    assert "scripts.lib.native_repair" not in phase1
    assert "scripts.lib.target_contract" not in phase1
    assert "scripts.lib.result_aggregation" not in phase1
    assert "from scripts.utils.gitcode import" in phase1
    assert "from scripts.harness.compose_pr import" in fork_delivery
    assert "from scripts.utils.gitcode import" in fork_delivery
    assert "from scripts.utils.gitcode import" in issue_lifecycle


def test_agent_execution_is_only_exposed_through_shared_harness():
    workflow = (
        ROOT / ".github" / "workflows" / "new-image.yml"
    ).read_text()

    assert "scripts/harness/run.py" in workflow
    assert "agent-generate" not in workflow
    assert "native-validate-repair" not in workflow


def test_workflow_uses_existing_validation_and_artifact_clis():
    workflow = (
        ROOT / ".github" / "workflows" / "new-image.yml"
    ).read_text()
    phase1 = (ROOT / "scripts" / "harness" / "phase1.py").read_text()

    assert "scripts/harness/gate_diff.py" in workflow
    assert "scripts/utils/artifacts.py" in workflow
    assert "phase1-native-validate" in workflow
    assert 'add_parser("target-validate")' not in phase1
    assert 'add_parser("results-aggregate")' not in phase1
    assert 'add_parser("native-validate")' not in phase1
