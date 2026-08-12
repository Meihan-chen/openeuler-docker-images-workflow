from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WATCH = ROOT / ".github" / "workflows" / "monitor_oe_upgrade_issues.yml"
WORKER = ROOT / ".github" / "workflows" / "oe_upgrade_worker.yml"
LEGACY = ROOT / ".github" / "workflows" / "upgrade_openeuler_versions.yml"
ROUND = ROOT / ".github" / "workflows" / "_create_new_image_rounds.yml"


def _load(path):
    return yaml.safe_load(path.read_text())


def _on(data):
    return data.get("on", data.get(True))


def test_watcher_has_plan_and_deliver_inputs_and_serial_concurrency():
    data = _load(WATCH)
    trigger = _on(data)

    assert set(trigger) == {"schedule", "workflow_dispatch"}
    assert trigger["schedule"][0]["cron"] == "*/30 * * * *"
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert set(inputs) == {
        "tracking_issue_number",
        "oe_version",
        "scope",
        "mode",
    }
    assert inputs["tracking_issue_number"]["required"] is True
    assert inputs["mode"]["options"] == ["plan", "deliver"]
    assert data["permissions"] == {"actions": "write", "contents": "read"}
    assert data["concurrency"]["cancel-in-progress"] is False
    text = WATCH.read_text()
    assert "oe-upgrade-advance" in text
    assert "oe_upgrade_worker.yml" in text
    assert "GITCODE_TOKEN" in text


def test_worker_is_one_task_per_run_with_stable_run_name_and_concurrency():
    data = _load(WORKER)
    trigger = _on(data)["workflow_dispatch"]["inputs"]

    assert set(trigger) == {
        "task_spec_json",
        "tracking_issue_number",
        "request_key",
        "base_sha",
        "task_display",
    }
    assert "oe-upgrade / ${{ inputs.task_display }}" == data["run-name"]
    assert "inputs.request_key" in data["concurrency"]["group"]
    assert data["concurrency"]["cancel-in-progress"] is False
    text = WORKER.read_text()
    assert "oe-upgrade-prepare" in text
    assert "oe-upgrade-test-prepare" in text
    assert text.count("./.github/workflows/_create_new_image_rounds.yml") == 4
    assert "architectures:" in text
    assert "task_key:" in text
    assert "--environment production" in text
    assert "--delivery-mode direct_branch_pr" in text
    assert "oe-upgrade-task-finalize" in text


def test_legacy_batch_upgrade_workflow_is_disabled_not_parallelized():
    data = _load(LEGACY)
    trigger = _on(data)

    assert set(trigger) == {"workflow_dispatch"}
    assert set(data["jobs"]) == {"disabled"}
    assert "_upgrade_versions.yml" not in LEGACY.read_text()
    assert "matrix" not in LEGACY.read_text()


def test_worker_passes_dynamic_architecture_and_task_namespace_to_rounds():
    data = _load(WORKER)
    for name in ("round-1", "round-2", "round-3", "round-4"):
        call = data["jobs"][name]
        assert call["uses"] == "./.github/workflows/_create_new_image_rounds.yml"
        assert call["with"]["architectures"] == (
            "${{ needs.prepare.outputs.architectures }}"
        )
        assert call["with"]["task_key"] == "${{ needs.prepare.outputs.task_key }}"

    round_data = _load(ROUND)
    assert "task_key" in _on(round_data)["workflow_call"]["inputs"]


def test_worker_uses_declared_architectures_for_package_and_cleanup():
    data = _load(WORKER)
    package = yaml.safe_dump(data["jobs"]["package-candidate"], width=10**6)

    assert "copy-declared-native-evidence" in package
    assert "candidate-create" in package
    assert "--request-key" in package
    assert "inputs.request_key" in package
    assert "contains(fromJSON(needs.prepare.outputs.architectures), 'x86_64')" in (
        data["jobs"]["release-x86-builders"]["if"]
    )
    assert "contains(fromJSON(needs.prepare.outputs.architectures), 'aarch64')" in (
        data["jobs"]["release-arm-builders"]["if"]
    )
