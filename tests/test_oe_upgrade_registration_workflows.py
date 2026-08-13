from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MONITOR_PATH = (
    ROOT / ".github" / "workflows" / "monitor_oe_upgrade_issues.yml"
)
WORKER_PATH = ROOT / ".github" / "workflows" / "oe_upgrade_worker.yml"


def _workflow(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _trigger(data: dict) -> dict:
    return data.get("on", data.get(True))


def test_monitor_registration_keeps_the_final_manual_input_contract():
    trigger = _trigger(_workflow(MONITOR_PATH))

    assert "workflow_dispatch" in trigger
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert set(inputs) == {
        "tracking_issue_number",
        "oe_version",
        "scope",
        "mode",
    }
    assert inputs["tracking_issue_number"]["required"] is True
    assert inputs["mode"]["type"] == "choice"
    assert inputs["mode"]["default"] == "plan"
    assert inputs["mode"]["options"] == ["plan", "deliver"]


def test_monitor_registration_is_fail_closed_and_has_no_side_effects():
    workflow = _workflow(MONITOR_PATH)
    text = MONITOR_PATH.read_text(encoding="utf-8")

    if "registration-only" not in workflow["jobs"]:
        assert "oe-upgrade-advance" in workflow["jobs"]
        return

    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"registration-only"}
    assert workflow["jobs"]["registration-only"]["runs-on"] == [
        "self-hosted",
        "Linux",
        "X64",
        "oe-image-x86",
    ]
    assert "exit 2" in text
    for forbidden in (
        "schedule:",
        "repository_dispatch:",
        "secrets.",
        "GITCODE_TOKEN",
        "gh workflow run",
    ):
        assert forbidden not in text


def test_worker_registration_keeps_the_final_manual_input_contract():
    trigger = _trigger(_workflow(WORKER_PATH))

    assert set(trigger) == {"workflow_dispatch"}
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert set(inputs) == {
        "task_spec_json",
        "tracking_issue_number",
        "request_key",
        "base_sha",
        "task_display",
    }
    assert all(spec["required"] is True for spec in inputs.values())


def test_worker_registration_is_fail_closed_and_has_no_side_effects():
    workflow = _workflow(WORKER_PATH)
    text = WORKER_PATH.read_text(encoding="utf-8")

    if "registration-only" not in workflow["jobs"]:
        assert "prepare" in workflow["jobs"]
        assert "finalize" in workflow["jobs"]
        return

    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"registration-only"}
    assert workflow["jobs"]["registration-only"]["runs-on"] == [
        "self-hosted",
        "Linux",
        "X64",
        "oe-image-x86",
    ]
    assert "exit 2" in text
    for forbidden in (
        "schedule:",
        "repository_dispatch:",
        "secrets.",
        "GITCODE_TOKEN",
        "gh workflow run",
    ):
        assert forbidden not in text
