import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "new-image.yml"
ACTIONLINT_CONFIG = ROOT / ".github" / "actionlint.yaml"


def _workflow():
    data = yaml.safe_load(WORKFLOW_PATH.read_text())
    assert isinstance(data, dict)
    return data


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
        "resume_x86",
        "resume_arm",
        "resume_revalidate_x86",
        "resume_package",
        "recover_package",
        "fork_pr",
        "failure_issue_contract_test",
    ]
    assert "validated_run_id" in trigger["workflow_dispatch"]["inputs"]
    assert "source_run_id" in trigger["workflow_dispatch"]["inputs"]
    assert "generation_run_id" in trigger["workflow_dispatch"]["inputs"]


def test_scenario_one_runs_full_validation_chain_and_delivers_same_run():
    jobs = _workflow()["jobs"]

    for job_name in (
        "prepare",
        "validate_x86",
        "validate_arm",
        "revalidate_x86",
        "package_candidate",
    ):
        assert "scenario_one" in jobs[job_name]["if"]

    delivery = jobs["deliver_fork_pr"]
    delivery_text = _job_text(delivery)
    assert delivery["needs"] == "package_candidate"
    assert "always()" in delivery["if"]
    assert "inputs.operation == 'scenario_one'" in delivery["if"]
    assert "needs.package_candidate.result == 'success'" in delivery["if"]
    assert "inputs.operation == 'fork_pr'" in delivery["if"]
    assert "inputs.operation == 'fork_pr'" in delivery_text
    assert "github.run_id" in delivery_text


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
    data = _workflow()
    jobs = data["jobs"]

    for name in (
        "prepare",
        "validate_x86",
        "revalidate_x86",
        "package_candidate",
        "deliver_fork_pr",
        "issue_contract_test",
    ):
        assert jobs[name]["runs-on"] == [
            "self-hosted",
            "Linux",
            "X64",
            "oe-image-x86",
        ]
    assert jobs["validate_arm"]["runs-on"] == [
        "self-hosted",
        "Linux",
        "ARM64",
        "oe-image-arm64",
    ]

    text = WORKFLOW_PATH.read_text()
    assert "docker/setup-qemu-action" not in text
    assert "docker/setup-buildx-action" not in text


def test_candidate_patch_converges_x86_then_arm_then_final_x86():
    jobs = _workflow()["jobs"]

    assert jobs["validate_x86"]["needs"] == "prepare"
    assert jobs["validate_arm"]["needs"] == "validate_x86"
    assert jobs["revalidate_x86"]["needs"] == "validate_arm"
    assert jobs["package_candidate"]["needs"] == "revalidate_x86"
    assert "scripts/harness/flow.py" in _job_text(jobs["validate_x86"])
    assert "scripts/harness/flow.py" in _job_text(jobs["validate_arm"])
    assert "scripts/harness/flow.py" in _job_text(jobs["revalidate_x86"])
    assert "scripts/harness/run.py" not in WORKFLOW_PATH.read_text()
    assert "phase1-native-repair" in _job_text(jobs["validate_x86"])
    assert "phase1-native-repair" in _job_text(jobs["validate_arm"])
    assert "phase1-native-validate" in _job_text(jobs["revalidate_x86"])


def test_resume_operations_reuse_failed_stage_artifacts():
    jobs = _workflow()["jobs"]
    x86 = _job_text(jobs["validate_x86"])
    arm = _job_text(jobs["validate_arm"])
    revalidate = _job_text(jobs["revalidate_x86"])
    package = _job_text(jobs["package_candidate"])

    assert "resume_x86" in x86
    assert "phase1-x86-${{ inputs.source_run_id }}" in x86
    assert "run-id: ${{ inputs.source_run_id }}" in x86
    assert "github-token: ${{ github.token }}" in x86

    assert "resume_arm" in arm
    assert "phase1-arm-${{ inputs.source_run_id }}" in arm
    assert "run-id: ${{ inputs.source_run_id }}" in arm
    assert "github-token: ${{ github.token }}" in arm

    assert "resume_x86" in arm
    assert "resume_x86" in revalidate
    assert "resume_arm" in revalidate
    assert "resume_revalidate_x86" in revalidate
    assert "phase1-arm-${{ inputs.source_run_id }}" in revalidate
    assert "run-id: ${{ inputs.source_run_id }}" in revalidate

    assert "resume_package" in package
    for artifact in (
        "phase1-generation-",
        "phase1-x86-",
        "phase1-arm-",
        "phase1-final-x86-",
    ):
        assert artifact in package
    assert "inputs.source_run_id" in package
    assert "phase1-resume-candidate-" in package
    assert WORKFLOW_PATH.read_text().count(
        "phase1-native-repair"
    ) == 2
    assert WORKFLOW_PATH.read_text().count(
        "phase1-native-validate"
    ) == 1
    assert WORKFLOW_PATH.read_text().count(
        "Enforce resumed candidate gate"
    ) == 1
    assert "steps.tools.outputs.jq_path" in package
    assert "aarch64.json" in revalidate
    assert ".checks == {" in revalidate
    for check in (
        "native_build",
        "dgoss",
        "shared_tests",
        "restart_persistence",
    ):
        assert f'"{check}": true' in WORKFLOW_PATH.read_text()
    assert '.status == "passed"' in WORKFLOW_PATH.read_text()
    assert "resume-provenance.json" in package
    assert '"promotable": false' in WORKFLOW_PATH.read_text()

    assert "always()" in jobs["validate_x86"]["if"]
    assert "needs.prepare.result == 'success'" in jobs["validate_x86"]["if"]
    assert "always()" in jobs["validate_arm"]["if"]
    assert (
        "needs.validate_x86.result == 'success'"
        in jobs["validate_arm"]["if"]
    )
    assert "always()" in jobs["revalidate_x86"]["if"]
    assert (
        "needs.validate_arm.result == 'success'"
        in jobs["revalidate_x86"]["if"]
    )
    assert "always()" in jobs["package_candidate"]["if"]
    assert "resume-candidate" not in _job_text(jobs["deliver_fork_pr"])


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

    for name in (
        "prepare",
        "validate_x86",
        "validate_arm",
        "revalidate_x86",
        "package_candidate",
    ):
        text = _job_text(jobs[name])
        assert "GITCODE_TOKEN" not in text
        assert "fork-deliver" not in text
        assert "issue-contract-test" not in text
    assert _workflow()["permissions"] == {
        "actions": "read",
        "contents": "read",
    }


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
    data = _workflow()
    uses = [
        step["uses"]
        for job in data["jobs"].values()
        for step in job.get("steps", [])
        if "uses" in step
    ]

    assert uses
    assert all(re.fullmatch(r"actions/[^@]+@[0-9a-f]{40}", use) for use in uses)
    text = WORKFLOW_PATH.read_text()
    assert "--require-hashes" in text
    assert ".github/python-phase1.lock.txt" in text


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
        "validate_x86": "Build, test, and repair on x86_64",
        "validate_arm": "Build, test, and repair on aarch64",
        "revalidate_x86": "Revalidate final candidate on x86_64",
        "package_candidate": "Verify and seal validated candidate",
        "deliver_fork_pr": "Promote validated candidate to fork PR",
        "issue_contract_test": "Exercise failure Issue lifecycle",
    }
    assert {
        job_id: job["name"]
        for job_id, job in data["jobs"].items()
    } == expected


def test_pipeline_smoke_reuses_candidate_chain_without_ai_or_gitcode_steps():
    jobs = _workflow()["jobs"]
    for job_name in (
        "prepare",
        "validate_x86",
        "validate_arm",
        "revalidate_x86",
        "package_candidate",
    ):
        assert "pipeline_smoke" in jobs[job_name]["if"]

    prepare_steps = {
        step["name"]: step for step in jobs["prepare"]["steps"]
    }
    smoke_generate = prepare_steps["Create deterministic smoke candidate"]
    assert "pipeline_smoke" in smoke_generate["if"]
    assert "phase1-smoke-generate" in smoke_generate["run"]
    assert "DEEPSEEK_API_KEY" not in _job_text(smoke_generate)

    for job_name in ("validate_x86", "validate_arm", "revalidate_x86"):
        smoke_steps = [
            step
            for step in jobs[job_name]["steps"]
            if step["name"].startswith("Run native pipeline smoke")
        ]
        assert len(smoke_steps) == 1
        assert "phase1-native-smoke" in smoke_steps[0]["run"]
        assert "DEEPSEEK_API_KEY" not in _job_text(smoke_steps[0])

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
    assert "target-create-patch" in diagnostic_patch["run"]
    assert "changes.patch" in diagnostic_patch["run"]

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


def test_native_jobs_upload_reports_and_diagnostic_patch_after_failure():
    jobs = _workflow()["jobs"]

    for job_name, label, directory in (
        ("validate_x86", "x86", "phase1-x86"),
        ("validate_arm", "ARM", "phase1-arm"),
    ):
        steps = {step["name"]: step for step in jobs[job_name]["steps"]}
        diagnostic = steps[f"Create {label} failure diagnostic patch"]
        assert "failure()" in diagnostic["if"]
        assert diagnostic["continue-on-error"] is True
        assert "target-create-patch" in diagnostic["run"]
        assert "changes.patch" in diagnostic["run"]

        upload_name = (
            "Upload x86-converged artifact"
            if job_name == "validate_x86"
            else "Upload ARM-converged artifact"
        )
        upload = steps[upload_name]
        assert "always()" in upload["if"]
        assert upload["with"]["path"] == (
            f"${{{{ runner.temp }}}}/{directory}/"
        )


def test_summary_markdown_does_not_trigger_single_quote_shellcheck_warning():
    text = WORKFLOW_PATH.read_text()

    assert "printf -- '- Candidate artifact:" not in text
    assert "printf -- '- Promotion input" not in text
