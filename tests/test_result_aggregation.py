import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

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


def _workspace(tmp_path):
    workspace = tmp_path / "target"
    (workspace / "Database" / "kvrocks").mkdir(parents=True)
    return workspace


def _environment(architecture):
    return {
        "test_time": "2026-07-28T12:00:00Z",
        "Model": f"{architecture}-runner",
        "architecture": architecture,
        "kernel": "6.6.0",
        "os": "openEuler 24.03 LTS-SP4",
        "cpu_model": f"{architecture}-cpu",
        "cpu_cores": 8,
        "software_name": "kvrocks",
        "software_version": "2.16.0",
        "python_version": "3.11.6",
        "numpy_version": "not-installed",
    }


CANDIDATE_DIGEST = "a" * 64
ROOT = Path(__file__).resolve().parents[1]


def _write_report(
    root,
    architecture,
    *,
    status="passed",
    validated_patch_sha256=CANDIDATE_DIGEST,
):
    payload = {
        "status": status,
        "task_id": _task().task_id,
        "architecture": architecture,
        "platform": "linux/amd64" if architecture == "x86_64" else "linux/arm64",
        "image_id": f"sha256:{architecture}",
        "validated_patch_sha256": validated_patch_sha256,
        "duration_seconds": 10.5,
        "environment": _environment(architecture),
        "checks": {
            "native_build": status == "passed",
            "runtime_test": status == "passed",
        },
    }
    (root / f"{architecture}.json").write_text(json.dumps(payload) + "\n")
    suite = ET.Element(
        "testsuite",
        {
            "name": architecture,
            "tests": "1",
            "failures": "0" if status == "passed" else "1",
            "errors": "0",
        },
    )
    ET.SubElement(suite, "testcase", {"name": "native"})
    ET.ElementTree(suite).write(
        root / f"{architecture}.junit.xml",
        encoding="utf-8",
        xml_declaration=True,
    )


def _reports(tmp_path):
    root = tmp_path / "native-reports"
    root.mkdir()
    _write_report(root, "x86_64")
    _write_report(root, "aarch64")
    return root


def test_keeps_schema_v1_results_in_production_output(tmp_path):
    from scripts.utils.artifacts import aggregate_native_results

    workspace = _workspace(tmp_path)
    reports = _reports(tmp_path)
    production_results = tmp_path / "candidate" / "reports" / "results.json"

    summary = aggregate_native_results(
        workspace=workspace,
        task=_task(),
        run_id="123456",
        run_url="https://github.com/Meihan-chen/repo/actions/runs/123456",
        report_dir=reports,
        results_output=production_results,
    )

    result_dir = (
        workspace
        / "Database"
        / "kvrocks"
        / "results"
        / "2.16.0"
        / "24.03-lts-sp4"
    )
    assert summary["status"] == "passed"
    assert summary["total_bytes"] <= 20 * 1024
    assert sorted(path.name for path in result_dir.iterdir()) == [
        "aarch64.junit.xml",
        "version_info.json",
        "x86_64.junit.xml",
    ]
    version_info = json.loads((result_dir / "version_info.json").read_text())
    assert len(version_info) == 11
    assert version_info["architecture"] == "x86_64,aarch64"
    assert version_info["cpu_cores"] == 16
    results = json.loads(production_results.read_text())
    assert results["status"] == "passed"
    assert results["validated_run_id"] == "123456"
    assert set(results["architectures"]) == {"x86_64", "aarch64"}
    assert results["artifact_url"].endswith("/123456")
    assert summary["results_file"] == str(production_results)


def test_aggregate_cli_routes_results_to_production_output(tmp_path):
    workspace = _workspace(tmp_path)
    reports = _reports(tmp_path)
    task_spec = tmp_path / "task-spec.json"
    task_spec.write_text(_task().to_json())
    production_results = tmp_path / "candidate" / "reports" / "results.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "utils" / "artifacts.py"),
            "aggregate-native",
            "--workspace",
            str(workspace),
            "--task-spec",
            str(task_spec),
            "--run-id",
            "123456",
            "--run-url",
            "https://example.test/actions/runs/123456",
            "--report-dir",
            str(reports),
            "--results-output",
            str(production_results),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert production_results.is_file()


def test_rejects_failed_architecture_without_writing_result_directory(tmp_path):
    from scripts.utils.artifacts import (
        ResultAggregationError,
        aggregate_native_results,
    )

    workspace = _workspace(tmp_path)
    reports = _reports(tmp_path)
    _write_report(reports, "aarch64", status="failed")

    with pytest.raises(ResultAggregationError, match="aarch64"):
        aggregate_native_results(
            workspace=workspace,
            task=_task(),
            run_id="123456",
            run_url="https://github.com/Meihan-chen/repo/actions/runs/123456",
            report_dir=reports,
            results_output=tmp_path / "production-results.json",
        )

    assert not (workspace / "Database" / "kvrocks" / "results").exists()


@pytest.mark.parametrize(
    "junit",
    (
        '<testsuite tests="0" failures="0" errors="0"/>',
        '<testsuite tests="1" failures="0" errors="0" skipped="1"/>',
        '<testsuite tests="1" failures="-1" errors="1"/>',
    ),
)
def test_rejects_junit_that_does_not_prove_executed_passes(tmp_path, junit):
    from scripts.utils.artifacts import (
        ResultAggregationError,
        aggregate_native_results,
    )

    workspace = _workspace(tmp_path)
    reports = _reports(tmp_path)
    (reports / "aarch64.junit.xml").write_text(junit)

    with pytest.raises(ResultAggregationError, match="aarch64 JUnit"):
        aggregate_native_results(
            workspace=workspace,
            task=_task(),
            run_id="123456",
            run_url="https://github.com/Meihan-chen/repo/actions/runs/123456",
            report_dir=reports,
            results_output=tmp_path / "production-results.json",
        )


@pytest.mark.parametrize(
    "checks",
    (
        {"native_build": True},
        {
            "native_build": True,
            "runtime_test": True,
            "invented_check": True,
        },
    ),
)
def test_rejects_noncanonical_native_check_sets(tmp_path, checks):
    from scripts.utils.artifacts import (
        ResultAggregationError,
        aggregate_native_results,
    )

    workspace = _workspace(tmp_path)
    reports = _reports(tmp_path)
    path = reports / "x86_64.json"
    payload = json.loads(path.read_text())
    payload["checks"] = checks
    path.write_text(json.dumps(payload))

    with pytest.raises(ResultAggregationError, match="checks are incomplete"):
        aggregate_native_results(
            workspace=workspace,
            task=_task(),
            run_id="123456",
            run_url="https://github.com/Meihan-chen/repo/actions/runs/123456",
            report_dir=reports,
            results_output=tmp_path / "production-results.json",
        )


def test_refuses_to_overwrite_existing_version_os_results(tmp_path):
    from scripts.utils.artifacts import (
        ResultAggregationError,
        aggregate_native_results,
    )

    workspace = _workspace(tmp_path)
    reports = _reports(tmp_path)
    existing = (
        workspace
        / "Database"
        / "kvrocks"
        / "results"
        / "2.16.0"
        / "24.03-lts-sp4"
    )
    existing.mkdir(parents=True)
    (existing / "owned").write_text("preserve\n")

    with pytest.raises(ResultAggregationError, match="already exists"):
        aggregate_native_results(
            workspace=workspace,
            task=_task(),
            run_id="123456",
            run_url="https://github.com/Meihan-chen/repo/actions/runs/123456",
            report_dir=reports,
            results_output=tmp_path / "production-results.json",
        )

    assert (existing / "owned").read_text() == "preserve\n"


def test_rejects_report_for_a_different_task(tmp_path):
    from scripts.utils.artifacts import (
        ResultAggregationError,
        aggregate_native_results,
    )

    workspace = _workspace(tmp_path)
    reports = _reports(tmp_path)
    report_path = reports / "aarch64.json"
    payload = json.loads(report_path.read_text())
    payload["task_id"] = "some-other-task"
    report_path.write_text(json.dumps(payload))

    with pytest.raises(ResultAggregationError, match="task"):
        aggregate_native_results(
            workspace=workspace,
            task=_task(),
            run_id="123456",
            run_url="https://github.com/Meihan-chen/repo/actions/runs/123456",
            report_dir=reports,
            results_output=tmp_path / "production-results.json",
        )


def test_rejects_architectures_that_validated_different_candidates(tmp_path):
    from scripts.utils.artifacts import (
        ResultAggregationError,
        aggregate_native_results,
    )

    workspace = _workspace(tmp_path)
    reports = _reports(tmp_path)
    # A repair on one architecture invalidates the other architecture's
    # earlier pass; job order alone cannot detect that.
    _write_report(reports, "aarch64", validated_patch_sha256="b" * 64)

    with pytest.raises(ResultAggregationError, match="different candidate"):
        aggregate_native_results(
            workspace=workspace,
            task=_task(),
            run_id="123456",
            run_url="https://github.com/Meihan-chen/repo/actions/runs/123456",
            report_dir=reports,
            results_output=tmp_path / "production-results.json",
        )

    assert not (
        workspace / "Database" / "kvrocks" / "results"
    ).exists()


def test_rejects_a_report_that_does_not_name_the_validated_candidate(tmp_path):
    from scripts.utils.artifacts import (
        ResultAggregationError,
        aggregate_native_results,
    )

    workspace = _workspace(tmp_path)
    reports = _reports(tmp_path)
    payload = json.loads((reports / "x86_64.json").read_text())
    del payload["validated_patch_sha256"]
    (reports / "x86_64.json").write_text(json.dumps(payload))

    with pytest.raises(ResultAggregationError, match="validated"):
        aggregate_native_results(
            workspace=workspace,
            task=_task(),
            run_id="123456",
            run_url="https://github.com/Meihan-chen/repo/actions/runs/123456",
            report_dir=reports,
            results_output=tmp_path / "production-results.json",
        )
