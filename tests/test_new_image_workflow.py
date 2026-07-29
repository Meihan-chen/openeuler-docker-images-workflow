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


def test_phase1_is_manual_only_with_three_explicit_operations():
    trigger = _trigger(_workflow())

    assert set(trigger) == {"workflow_dispatch"}
    operation = trigger["workflow_dispatch"]["inputs"]["operation"]
    assert operation["type"] == "choice"
    assert operation["default"] == "validate_only"
    assert operation["options"] == [
        "validate_only",
        "fork_pr",
        "failure_issue_contract_test",
    ]
    assert "validated_run_id" in trigger["workflow_dispatch"]["inputs"]


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
    assert "scripts/harness/run.py" in _job_text(jobs["validate_x86"])
    assert "scripts/harness/run.py" in _job_text(jobs["validate_arm"])
    assert "phase1-native-repair" in _job_text(jobs["validate_x86"])
    assert "phase1-native-repair" in _job_text(jobs["validate_arm"])
    assert "phase1-native-validate" in _job_text(jobs["revalidate_x86"])


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


def test_summary_markdown_does_not_trigger_single_quote_shellcheck_warning():
    text = WORKFLOW_PATH.read_text()

    assert "printf -- '- Candidate artifact:" not in text
    assert "printf -- '- Promotion input" not in text
