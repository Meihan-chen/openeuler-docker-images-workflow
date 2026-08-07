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


def test_phase_one_candidate_and_delivery_modules_are_consolidated():
    lib = ROOT / "scripts" / "lib"
    candidate = (lib / "candidate_bundle.py").read_text()
    delivery = (lib / "pr_delivery.py").read_text()

    for obsolete in (
        "candidate_promotion.py",
        "delivery_config.py",
        "fork_pr_pipeline.py",
        "git_delivery.py",
    ):
        assert not (lib / obsolete).exists()

    gitcode = (lib / "gitcode_client.py").read_text()
    assert "def promote_candidate(" in candidate
    assert "class DeliveryConfig:" in gitcode
    assert "def deliver_validated_candidate(" in delivery
    assert "def push_working_branch(" in delivery
    assert "scripts.harness" not in delivery
    assert "scripts.utils" not in delivery


def test_runner_and_smoke_helpers_live_with_their_owning_pipeline():
    lib = ROOT / "scripts" / "lib"
    toolchain = (lib / "toolchain.py").read_text()
    generation = (lib / "generation_pipeline.py").read_text()

    assert not (lib / "runner_preflight.py").exists()
    assert not (lib / "smoke_candidate.py").exists()
    assert "def evaluate_preflight(" in toolchain
    assert "def write_smoke_candidate(" in generation


def test_lib_layer_never_imports_cli_facades():
    lib = ROOT / "scripts" / "lib"

    for path in lib.glob("*.py"):
        source = path.read_text()
        assert "from scripts.harness" not in source, path.name
        assert "from scripts.utils" not in source, path.name


def test_agent_execution_is_exposed_through_flow_orchestrator():
    # Every job lives in the shared spine; the entries only pass inputs.
    workflow = (
        ROOT / ".github" / "workflows" / "_create_new_image_pipeline.yml"
    ).read_text()

    assert "scripts/harness/flow.py" in workflow
    assert "scripts/harness/run.py" not in workflow
    assert "scripts/harness/phase1.py" not in workflow
    assert "agent-generate" not in workflow
    assert "native-validate-repair" not in workflow


def test_legacy_harness_also_uses_only_the_app_shared_test_entrypoint(
    tmp_path,
    monkeypatch,
):
    from scripts.harness.create_demo import create_demo
    from scripts.harness.run import _build_creator_prompt

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TARGET_REPO_DIR", str(tmp_path))
    monkeypatch.setenv("PACKAGE", "nginx")
    monkeypatch.setenv("APP_VERSION", "1.27.2")
    monkeypatch.setenv("OS_VERSION", "24.03-lts")
    monkeypatch.setenv("DOMAIN", "Cloud")

    create_demo("nginx", "1.27.2", "24.03-lts", "Cloud")
    prompt = _build_creator_prompt("testcase", round_num=1)

    assert not (
        tmp_path / "Cloud" / "nginx" / "1.27.2" / "24.03-lts" / "test.sh"
    ).exists()
    assert (
        tmp_path / "Cloud" / "nginx" / "tests" / "test.sh"
    ).is_file()
    assert "tests/ (goss.yaml, goss_wait.yaml, test_helpers.sh, test.sh)" in (
        prompt
    )
    assert "alongside the Dockerfile" not in prompt


def test_workflow_uses_existing_validation_and_artifact_clis():
    workflows = ROOT / ".github" / "workflows"
    workflow = (workflows / "_create_new_image_pipeline.yml").read_text()
    # Native validation moved into the reusable round the pipeline calls.
    rounds = (workflows / "_create_new_image_rounds.yml").read_text()
    flow = (ROOT / "scripts" / "harness" / "flow.py").read_text()

    assert "scripts/harness/gate_diff.py" in workflow
    assert "scripts/utils/artifacts.py" in workflow
    assert "phase1-native-validate" in rounds
    assert "./.github/workflows/_create_new_image_rounds.yml" in workflow
    entry = (workflows / "create_new_images.yml").read_text()
    assert "./.github/workflows/_create_new_image_pipeline.yml" in entry
    assert 'add_parser("target-validate")' not in flow
    assert 'add_parser("results-aggregate")' not in flow
    assert 'add_parser("native-validate")' not in flow
