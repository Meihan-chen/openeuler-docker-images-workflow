import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "new-image.yml"
ROUND_PATH = ROOT / ".github" / "workflows" / "phase1-round.yml"
DECIDE_PATH = ROOT / ".github" / "workflows" / "phase1-decide.yml"
ROUNDS = ("round1", "round2", "round3", "round4")
DECISIONS = ("decide1", "decide2", "decide3", "decide4")
ACTIONLINT_CONFIG = ROOT / ".github" / "actionlint.yaml"
ACTIONS_DIR = ROOT / ".github" / "actions"
PHASE1_ACTIONS = ("phase1-setup", "phase1-replay", "phase1-emit-patch")


def _workflow(path=None):
    data = yaml.safe_load((path or WORKFLOW_PATH).read_text())
    assert isinstance(data, dict)
    return data


def _action_path(name):
    return ACTIONS_DIR / name / "action.yml"


def _action(name):
    data = yaml.safe_load(_action_path(name).read_text())
    assert isinstance(data, dict)
    return data


def _action_text(name):
    return _action_path(name).read_text()


def _trigger(data):
    return data.get("on", data.get(True))


def _job_text(job):
    return yaml.safe_dump(job, sort_keys=True)


def test_phase1_is_manual_only_with_explicit_operations():
    trigger = _trigger(_workflow())

    assert set(trigger) == {"workflow_dispatch"}
    operation = trigger["workflow_dispatch"]["inputs"]["operation"]
    assert operation["type"] == "choice"
    assert operation["default"] == "pipeline_smoke"
    assert operation["options"] == [
        "pipeline_smoke",
        "validate_only",
        "scenario_one",
        "resume_round",
        "resume_package",
        "recover_package",
        "fork_pr",
        "failure_issue_contract_test",
    ]
    assert "validated_run_id" in trigger["workflow_dispatch"]["inputs"]
    assert "source_run_id" in trigger["workflow_dispatch"]["inputs"]
    assert "generation_run_id" in trigger["workflow_dispatch"]["inputs"]
    assert "resume_from_round" in trigger["workflow_dispatch"]["inputs"]



def test_scenario_one_runs_full_validation_chain_and_delivers_same_run():
    jobs = _workflow()["jobs"]

    assert "scenario_one" in jobs["prepare"]["if"]
    # Sealing is gated on convergence, not on which operation asked for it.
    assert "converged == 'true'" in jobs["package_candidate"]["if"]

    delivery = jobs["deliver_fork_pr"]
    delivery_text = _job_text(delivery)
    assert delivery["needs"] == "package_candidate"
    assert "always()" in delivery["if"]
    assert "inputs.operation == 'scenario_one'" in delivery["if"]
    assert "needs.package_candidate.result == 'success'" in delivery["if"]
    assert "inputs.operation == 'fork_pr'" in delivery["if"]
    assert "github.run_id" in delivery_text

    prepare_steps = {step["name"]: step for step in jobs["prepare"]["steps"]}
    assert (
        "scenario_one"
        in prepare_steps["Generate and review candidate content"]["if"]
    )
    # Rounds are operation-agnostic: they validate whatever prepare staged.
    assert jobs["round1"]["with"]["operation"] == "${{ inputs.operation }}"


def test_phase1_task_defaults_are_the_confirmed_kvrocks_contract():
    inputs = _trigger(_workflow())["workflow_dispatch"]["inputs"]

    assert inputs["app"]["default"] == "kvrocks"
    assert inputs["version"]["default"] == "2.16.0"
    assert inputs["os_version"]["default"] == "24.03-lts-sp4"
    assert inputs["domain"]["default"] == "Database"
    assert inputs["source_url"]["default"] == (
        "https://github.com/apache/kvrocks/tree/v2.16.0"
    )



def test_native_jobs_use_exact_self_hosted_labels_and_no_emulation_actions():
    jobs = _workflow()["jobs"]
    round_jobs = _workflow(ROUND_PATH)["jobs"]

    for name in (
        "prepare",
        "seed_resume",
        "package_candidate",
        "release_x86_builders",
        "deliver_fork_pr",
        "issue_contract_test",
    ):
        assert jobs[name]["runs-on"] == [
            "self-hosted",
            "Linux",
            "X64",
            "oe-image-x86",
        ]
    assert jobs["release_arm_builders"]["runs-on"] == [
        "self-hosted",
        "Linux",
        "ARM64",
        "oe-image-arm64",
    ]
    # Each round pins both native runners; emulation would hide the very
    # architecture differences the round exists to find.
    assert round_jobs["x86_64"]["runs-on"] == [
        "self-hosted",
        "Linux",
        "X64",
        "oe-image-x86",
    ]
    assert round_jobs["aarch64"]["runs-on"] == [
        "self-hosted",
        "Linux",
        "ARM64",
        "oe-image-arm64",
    ]
    assert _workflow(DECIDE_PATH)["jobs"]["decide"]["runs-on"] == [
        "self-hosted",
        "Linux",
        "X64",
        "oe-image-x86",
    ]

    for path in (WORKFLOW_PATH, ROUND_PATH, DECIDE_PATH):
        text = path.read_text()
        assert "docker/setup-qemu-action" not in text
        assert "docker/setup-buildx-action" not in text



def test_every_round_validates_one_candidate_on_both_architectures():
    jobs = _workflow()["jobs"]

    assert jobs["round1"]["needs"] == ["prepare", "seed_resume"]
    for index, name in enumerate(ROUNDS):
        assert jobs[name]["uses"] == "./.github/workflows/phase1-round.yml"
        assert jobs[name]["with"]["round"] == str(index + 1)
    for index, name in enumerate(DECISIONS):
        decision = jobs[name]
        assert decision["uses"] == "./.github/workflows/phase1-decide.yml"
        assert decision["needs"] == ROUNDS[index]
        assert decision["with"]["round"] == str(index + 1)
        # pipeline_smoke converges deterministically; giving it a repair
        # budget would burn model calls the Fixer cannot possibly help with,
        # because the smoke image is built from a synthetic context.
        assert decision["with"]["max_rounds"] == (
            "${{ inputs.operation == 'pipeline_smoke' && '0' || '3' }}"
        )
        # GitHub expressions have no arithmetic, so the next round is explicit.
        assert decision["with"]["next_round"] == str(index + 2)

    # A later round exists only because the previous one did not converge.
    for index, name in enumerate(ROUNDS[1:]):
        condition = jobs[name]["if"]
        assert f"needs.{DECISIONS[index]}.result == 'success'" in condition
        assert (
            f"needs.{DECISIONS[index]}.outputs.converged != 'true'"
            in condition
        )

    workflow_text = WORKFLOW_PATH.read_text()
    assert "scripts/harness/run.py" not in workflow_text
    # The serial chain and its extra revalidation job are gone.
    assert "revalidate" not in workflow_text
    assert "phase1-native-repair" not in workflow_text



def test_resume_restarts_one_round_from_another_run_of_the_same_pipeline():
    jobs = _workflow()["jobs"]
    seed = jobs["seed_resume"]
    seed_text = _job_text(seed)

    assert seed["if"] == "${{ inputs.operation == 'resume_round' }}"
    assert "phase1-patch${{ inputs.resume_from_round }}-${{" in seed_text
    assert "run-id: ${{ inputs.source_run_id }}" in seed_text
    assert "github-token: ${{ github.token }}" in seed_text
    # Republished under this run so every round reads one uniform name.
    assert "phase1-patch${{ inputs.resume_from_round }}-${{ github.run_id }}" in (
        seed_text
    )

    for index, name in enumerate(ROUNDS):
        condition = jobs[name]["if"]
        assert "needs.seed_resume.result == 'success'" in condition
        assert f"inputs.resume_from_round == '{index + 1}'" in condition

    package = _job_text(jobs["package_candidate"])
    assert "resume_package" in package
    assert "phase1-resume-candidate-" in package
    assert "resume-provenance.json" in package
    assert '"promotable": false' in WORKFLOW_PATH.read_text()


def test_recover_package_combines_generation_and_validation_runs():
    jobs = _workflow()["jobs"]
    package = jobs["package_candidate"]
    text = _job_text(package)

    assert "recover_package" in package["if"]
    assert "inputs.generation_run_id" in text
    assert "inputs.source_run_id" in text
    assert "target-apply-recovered-patch" in text
    assert "recovery-provenance.json" in text
    assert text.count("cmp ") == 4
    assert "generation/task-spec.json" in text
    assert "generation/base-sha.txt" in text
    assert "promotable: true" in WORKFLOW_PATH.read_text()
    assert "phase1-candidate-" in text
    assert "phase1-resume-candidate-" in text
    assert "DEEPSEEK_API_KEY" not in text
    assert "phase1-native-repair" not in text
    assert "phase1-native-validate" not in text


def test_recover_package_only_accepts_confirmed_full_validation_lineage():
    package = _job_text(_workflow()["jobs"]["package_candidate"])
    workflow_text = WORKFLOW_PATH.read_text()

    assert "30478803960" in package
    assert "30483501656" in package
    for digest in (
        "3b9579c664c1a699121156d48384d43647a8ac6671490469d1189d788308a56f",
        "65682e11649b4e992ee000872317f2b4d04786e1c56a233549dd2c8b2222fc37",
        "afad06c7206854c432cba89c1a19ffd9836a79281763cd1736f01f3e0aae729d",
        "8b046be0392a36dccb28b1d14f2c504c5b95fb6e3f518809731639d5e2c7ce86",
    ):
        assert digest in package
    assert package.count(".checks == {") >= 2
    for check in (
        "native_build",
        "dgoss",
        "shared_tests",
        "restart_persistence",
    ):
        assert workflow_text.count(f'"{check}": true') >= 2
    assert 'evidence_run_id="${{ inputs.source_run_id }}"' in workflow_text
    assert '--run-id "${evidence_run_id}"' in workflow_text
    assert '--expected-run-id "${evidence_run_id}"' in workflow_text



def test_validate_only_jobs_have_no_gitcode_credential_or_write_command():
    jobs = _workflow()["jobs"]

    for name in ("prepare", "seed_resume", "package_candidate"):
        text = _job_text(jobs[name])
        assert "GITCODE_TOKEN" not in text
        assert "fork-deliver" not in text
        assert "issue-contract-test" not in text
    for path in (ROUND_PATH, DECIDE_PATH):
        text = path.read_text()
        assert "GITCODE_TOKEN" not in text
        assert "fork-deliver" not in text
        assert "issue-contract-test" not in text
    for name in PHASE1_ACTIONS:
        text = _action_text(name)
        assert "GITCODE_TOKEN" not in text
        assert "fork-deliver" not in text
        assert "issue-contract-test" not in text
    assert _workflow()["permissions"] == {
        "actions": "read",
        "contents": "read",
    }


def test_only_the_decision_stage_receives_a_model_key():
    jobs = _workflow()["jobs"]

    # Rounds only build and test, so handing them a key would widen the blast
    # radius for nothing; secrets: inherit would do exactly that.
    assert "secrets" not in jobs["round1"]
    assert ROUND_PATH.read_text().count("DEEPSEEK_API_KEY") == 0
    for name in DECISIONS:
        key = jobs[name]["secrets"]["DEEPSEEK_API_KEY"]
        assert "secrets.DEEPSEEK_API_KEY" in key
        # The smoke path must reach the decision stage with no key at all.
        assert "pipeline_smoke" in key
    assert "inherit" not in WORKFLOW_PATH.read_text()


def test_the_smoke_path_can_never_reach_the_fixer():
    jobs = _workflow()["jobs"]

    for name in DECISIONS:
        assert "pipeline_smoke" in jobs[name]["with"]["max_rounds"]
        assert "'0'" in jobs[name]["with"]["max_rounds"]
    # A zero budget makes decide_round raise before it ever builds a prompt,
    # so a flaky smoke build fails loudly instead of silently repairing.
    decide_call = _trigger(_workflow(DECIDE_PATH))["workflow_call"]
    assert decide_call["secrets"]["DEEPSEEK_API_KEY"]["required"] is False


def test_fork_pr_reuses_named_artifact_from_exact_validated_run():
    job = _workflow()["jobs"]["deliver_fork_pr"]
    text = _job_text(job)

    assert "inputs.operation == 'fork_pr'" in job["if"]
    assert "inputs.validated_run_id" in text
    assert "phase1-candidate-" in text
    assert "run-id" in text
    assert "fork-deliver" in text
    assert "--delivery-run-id" in text
    assert "github.run_id" in text
    assert "--delivery-run-attempt" in text
    assert "github.run_attempt" in text
    assert "GITCODE_TOKEN" in text
    assert "--token" not in text


def test_issue_probe_is_isolated_and_explicit():
    jobs = _workflow()["jobs"]
    issue = jobs["issue_contract_test"]

    assert "failure_issue_contract_test" in issue["if"]
    assert "issue-contract-test" in _job_text(issue)
    assert "GITCODE_TOKEN" in _job_text(issue)
    assert "GITCODE_TOKEN" in _job_text(jobs["deliver_fork_pr"])


def test_actions_are_commit_pinned_and_python_install_requires_hashes():
    uses = [
        step["uses"]
        for job in _workflow()["jobs"].values()
        for step in job.get("steps", [])
        if "uses" in step
    ]
    for name in PHASE1_ACTIONS:
        uses.extend(
            step["uses"]
            for step in _action(name)["runs"]["steps"]
            if "uses" in step
        )

    assert uses
    external = [use for use in uses if not use.startswith("./")]
    local = [use for use in uses if use.startswith("./")]
    assert external
    assert all(
        re.fullmatch(r"actions/[^@]+@[0-9a-f]{40}", use) for use in external
    )
    assert local
    assert all(
        (ROOT / use.removeprefix("./") / "action.yml").is_file()
        for use in local
    )

    setup = _action_text("phase1-setup")
    assert "--require-hashes" in setup
    assert ".github/python-phase1.lock.txt" in setup


def test_legacy_evidence_is_allowed_only_on_the_reviewed_recovery_path():
    workflow_text = WORKFLOW_PATH.read_text()
    aggregate = next(
        step
        for step in _workflow()["jobs"]["package_candidate"]["steps"]
        if step.get("name") == "Aggregate dual-architecture result evidence"
    )["run"]

    # Reports predating validated_patch_sha256 may only be accepted for the
    # one reviewed run whose report bytes are already pinned by SHA256.
    assert workflow_text.count("--allow-legacy-evidence") == 1
    assert 'legacy_evidence=""' in aggregate
    assert '"${{ inputs.operation }}" = "recover_package"' in aggregate
    assert "TODO(test-recovery-cleanup)" in aggregate
    assert "${legacy_evidence}" in aggregate



def test_each_architecture_hands_its_run_builder_back_exactly_once():
    jobs = _workflow()["jobs"]
    workflow_text = WORKFLOW_PATH.read_text()

    # Validation keeps the builder alive for later rounds, so the run is only
    # leak-free if a release job runs even when validation failed.
    for name, architecture, runner_label in (
        ("release_x86_builders", "x86_64", "oe-image-x86"),
        ("release_arm_builders", "aarch64", "oe-image-arm64"),
    ):
        job = jobs[name]
        text = _job_text(job)
        assert set(ROUNDS).issubset(job["needs"])
        assert "always()" in job["if"]
        assert "skipped" in job["if"]
        assert runner_label in job["runs-on"]
        assert "phase1-native-release" in text
        assert f"--architecture {architecture}" in text
        assert "github.run_id" in text
        assert "GITCODE_TOKEN" not in text

    assert workflow_text.count("phase1-native-release") == 2
    for path in (WORKFLOW_PATH, ROUND_PATH, DECIDE_PATH):
        assert "docker buildx rm" not in path.read_text()


def test_test_duplicate_pr_skip_has_an_explicit_production_removal_marker():
    text = WORKFLOW_PATH.read_text()

    assert "TODO(production-duplicate-pr-guard)" in text
    assert "duplicate PR" in text


def test_actionlint_knows_the_confirmed_custom_runner_labels():
    config = yaml.safe_load(ACTIONLINT_CONFIG.read_text())

    assert config["self-hosted-runner"]["labels"] == [
        "oe-image-x86",
        "oe-image-arm64",
    ]



def test_jobs_and_run_have_readable_display_names():
    data = _workflow()

    assert "inputs.operation" in data["run-name"]
    assert "inputs.app" in data["run-name"]
    expected = {
        "prepare": "Generate candidate on x86_64",
        "seed_resume": "Seed resumed round candidate",
        "round1": "Round 1",
        "decide1": "Decision 1",
        "round2": "Round 2",
        "decide2": "Decision 2",
        "round3": "Round 3",
        "decide3": "Decision 3",
        "round4": "Round 4",
        "decide4": "Decision 4",
        "package_candidate": "Verify and seal validated candidate",
        "release_x86_builders": "Release x86_64 run builders",
        "release_arm_builders": "Release aarch64 run builders",
        "deliver_fork_pr": "Promote validated candidate to fork PR",
        "issue_contract_test": "Exercise failure Issue lifecycle",
    }
    assert {
        job_id: job["name"] for job_id, job in data["jobs"].items()
    } == expected



def test_pipeline_smoke_reuses_candidate_chain_without_ai_or_gitcode_steps():
    jobs = _workflow()["jobs"]
    assert "pipeline_smoke" in jobs["prepare"]["if"]
    assert "converged == 'true'" in jobs["package_candidate"]["if"]

    prepare_steps = {step["name"]: step for step in jobs["prepare"]["steps"]}
    smoke_generate = prepare_steps["Create deterministic smoke candidate"]
    assert "pipeline_smoke" in smoke_generate["if"]
    assert "phase1-smoke-generate" in smoke_generate["run"]
    assert "DEEPSEEK_API_KEY" not in _job_text(smoke_generate)

    # The smoke candidate always passes, so decide1 converges and rounds 2-4
    # skip themselves; the same round jobs serve both paths.
    round_jobs = _workflow(ROUND_PATH)["jobs"]
    for job in round_jobs.values():
        smoke_steps = [
            step
            for step in job["steps"]
            if step.get("name", "").startswith("Run native pipeline smoke")
        ]
        assert len(smoke_steps) == 1
        assert "phase1-native-smoke" in smoke_steps[0]["run"]
        assert "pipeline_smoke" in smoke_steps[0]["if"]

    package_text = _job_text(jobs["package_candidate"])
    assert "phase1-smoke-candidate-" in package_text
    assert "not promotable" in package_text


def test_prepare_uploads_reports_and_diagnostic_patch_after_generation_failure():
    prepare = _workflow()["jobs"]["prepare"]
    steps = {step["name"]: step for step in prepare["steps"]}

    diagnostic_patch = steps["Create failure diagnostic patch"]
    assert "failure()" in diagnostic_patch["if"]
    assert "steps.base.outputs.sha != ''" in diagnostic_patch["if"]
    assert diagnostic_patch["continue-on-error"] is True
    assert diagnostic_patch["uses"] == "./.github/actions/phase1-emit-patch"
    assert diagnostic_patch["with"]["tolerate-missing"] == "true"

    upload = steps["Upload generation artifact"]
    assert "always()" in upload["if"]
    assert "steps.base.outputs.sha != ''" in upload["if"]
    assert upload["with"]["path"] == "${{ runner.temp }}/phase1-prepare/"


def test_hadolint_only_ignores_unpinned_dnf_packages():
    jobs = _workflow()["jobs"]
    prepare_steps = {
        step["name"]: step for step in jobs["prepare"]["steps"]
    }
    package_steps = {
        step["name"]: step for step in jobs["package_candidate"]["steps"]
    }

    for step in (
        prepare_steps["Lint generated Dockerfile"],
        package_steps["Enforce final target gates and lint"],
    ):
        assert "--ignore DL3041" in step["run"]

    assert WORKFLOW_PATH.read_text().count("--ignore DL3041") == 2


def test_validate_only_lints_before_agent_qa_inside_generation():
    prepare_steps = {
        step["name"]: step
        for step in _workflow()["jobs"]["prepare"]["steps"]
    }

    generate = prepare_steps["Generate and review candidate content"]
    assert "--hadolint" in generate["run"]
    assert "steps.tools.outputs.hadolint_path" in generate["run"]

    standalone_lint = prepare_steps["Lint generated Dockerfile"]
    assert standalone_lint["if"] == "${{ inputs.operation == 'pipeline_smoke' }}"



def test_a_failed_round_still_publishes_the_evidence_the_decision_needs():
    round_jobs = _workflow(ROUND_PATH)["jobs"]

    for name, architecture in (("x86_64", "x86_64"), ("aarch64", "aarch64")):
        steps = {step.get("name"): step for step in round_jobs[name]["steps"]}
        validate = steps[f"Validate natively on {architecture}"]
        # The decision stage rules on the round, so a failed build must not
        # fail the job before its report is uploaded.
        assert validate["continue-on-error"] is True
        upload = steps[f"Upload {architecture} round evidence"]
        assert "always()" in upload["if"]
        assert upload["with"]["name"] == (
            "phase1-round${{ inputs.round }}-"
            f"{architecture}" + "-${{ github.run_id }}"
        )
        assert upload["with"]["if-no-files-found"] == "error"


def test_summary_markdown_does_not_trigger_single_quote_shellcheck_warning():
    text = WORKFLOW_PATH.read_text()

    assert "printf -- '- Candidate artifact:" not in text
    assert "printf -- '- Promotion input" not in text


def test_shared_stage_steps_live_in_one_composite_action_each():
    workflow_text = WORKFLOW_PATH.read_text()

    for name in PHASE1_ACTIONS:
        assert _action(name)["runs"]["using"] == "composite"

    # Every shared step body exists exactly once, inside its action.
    for fragment in (
        "python3 -m venv",
        "scripts/bootstrap_tools.py",
        "scripts/runner_preflight.py",
        "target-apply-patch",
    ):
        assert fragment not in workflow_text

    setup = _action_text("phase1-setup")
    assert "python3 -m venv" in setup
    assert "scripts/bootstrap_tools.py" in setup
    assert "scripts/runner_preflight.py" in setup
    assert "target-apply-patch" in _action_text("phase1-replay")
    assert "target-create-patch" in _action_text("phase1-emit-patch")


def _delegated_setup_step(job):
    steps = job["steps"]
    checkout = next(
        index
        for index, step in enumerate(steps)
        if step.get("uses", "").startswith("actions/checkout@")
    )
    # A local action is only resolvable after the repository is checked out,
    # so checkout must stay in the job and precede every local action. Cheap
    # input guards may run first so bad input fails before any provisioning.
    assert all(
        not step.get("uses", "").startswith("./") for step in steps[:checkout]
    )
    setup = steps[checkout + 1]
    assert setup["uses"] == "./.github/actions/phase1-setup"
    return setup



def test_every_native_job_delegates_setup_and_keeps_checkout_in_the_job():
    jobs = _workflow()["jobs"]

    for name, arch, preflight in (
        ("prepare", "x86_64", True),
        ("package_candidate", "x86_64", False),
    ):
        setup = _delegated_setup_step(jobs[name])
        assert setup["id"] == "tools"
        assert setup["with"]["arch"] == arch
        assert setup["with"].get("preflight", "false") == str(preflight).lower()

    for job in _workflow(ROUND_PATH)["jobs"].values():
        setup = _delegated_setup_step(job)
        assert setup["with"]["preflight"] == "true"
    decide_setup = _delegated_setup_step(_workflow(DECIDE_PATH)["jobs"]["decide"])
    assert decide_setup["with"]["arch"] == "x86_64"

    for name in ("deliver_fork_pr", "issue_contract_test"):
        setup = _delegated_setup_step(jobs[name])
        assert "with" not in setup



def test_replay_action_always_pins_the_exact_validated_base_sha():
    replay = _action_text("phase1-replay")

    assert "--expected-sha" in replay
    assert "--branch master" in replay
    # A plain clone would silently drift onto a newer target master.
    assert replay.count("target-clone") == 1

    replays = [
        step
        for path in (WORKFLOW_PATH, ROUND_PATH, DECIDE_PATH)
        for job in _workflow(path)["jobs"].values()
        for step in job.get("steps", [])
        if step.get("uses") == "./.github/actions/phase1-replay"
    ]
    assert len(replays) == 4
    assert all("input-dir" in step["with"] for step in replays)


def test_emit_patch_action_refuses_a_missing_base_sha_by_default():
    emit = _action(_action_path("phase1-emit-patch").parent.name)
    body = emit["runs"]["steps"][0]["run"]

    assert emit["inputs"]["tolerate-missing"]["default"] == "false"
    assert "exit 2" in body
    assert "exit 0" in body
    assert 'if [ "${TOLERATE_MISSING}" = "true" ]' in body
