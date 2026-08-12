import json
from pathlib import Path

import pytest


BASE_SHA = "1d49c0858d8d8152acb1bd3caf5cd862b091160f"


def _task():
    from scripts.lib.task_spec import TaskSpec

    return TaskSpec.from_workflow_dispatch(
        {
            "app": "kvrocks",
            "version": "2.16.0",
            "os_version": "24.03-lts-sp4",
            "domain": "Database",
            "source_url": "https://github.com/apache/kvrocks",
        }
    )


def _oe_task():
    from scripts.lib.task_spec import TaskSpec

    return TaskSpec.from_workflow_dispatch(
        {
            "schema_version": 2,
            "scenario": "oe-upgrade",
            "app": "redis",
            "image_name": "redis",
            "version": "8.2.1",
            "os_version": "26.03-lts",
            "domain": "Database",
            "source_url": "",
            "mdu_path": "Database/redis",
            "derive_from": "8.2.1/24.03-lts-sp1",
            "architectures": ["x86_64"],
        }
    )


def _payload(root: Path):
    (root / "reports" / "agents").mkdir(parents=True)
    (root / "changes.patch").write_text("diff --git a/a b/a\n")
    report = {
        "status": "passed",
        "checks": {
            "native_build": True,
            "runtime_test": True,
        },
    }
    (root / "reports" / "x86_64.json").write_text(json.dumps(report) + "\n")
    (root / "reports" / "aarch64.json").write_text(json.dumps(report) + "\n")
    for architecture in ("x86_64", "aarch64"):
        (root / "reports" / f"{architecture}.junit.xml").write_text(
            '<testsuite tests="1" failures="0" errors="0"/>'
        )
    gate = '{"status":"passed","delivery_allowed":true}\n'
    (root / "reports" / "gates.json").write_text(gate)
    (root / "reports" / "generation-gates.json").write_text(gate)
    (root / "reports" / "hadolint.txt").write_text("")
    (root / "reports" / "results.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "passed",
                "task_id": _task().task_id,
                "validated_run_id": "123456",
                "artifact_url": "https://example.test/actions/runs/123456",
                "architectures": {
                    architecture: {
                        "checks": report["checks"],
                    }
                    for architecture in ("x86_64", "aarch64")
                },
            }
        )
        + "\n"
    )


def test_candidate_bundle_records_task_base_run_and_checksums(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundle

    _payload(tmp_path)
    bundle = CandidateBundle.create(
        tmp_path,
        task=_task(),
        base_sha=BASE_SHA,
        validated_run_id="123456",
    )

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert bundle.manifest.task_id == _task().task_id
    assert manifest["base_sha"] == BASE_SHA
    assert manifest["validated_run_id"] == "123456"
    assert sorted(manifest["files"]) == [
        "changes.patch",
        "reports/aarch64.json",
        "reports/aarch64.junit.xml",
        "reports/gates.json",
        "reports/generation-gates.json",
        "reports/hadolint.txt",
        "reports/results.json",
        "reports/x86_64.json",
        "reports/x86_64.junit.xml",
        "task-spec.json",
    ]
    assert len(manifest["content_sha256"]) == 64


def test_candidate_bundle_verifies_untouched_payload(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundle

    _payload(tmp_path)
    CandidateBundle.create(
        tmp_path,
        task=_task(),
        base_sha=BASE_SHA,
        validated_run_id="123456",
    )

    verified = CandidateBundle.verify(tmp_path, expected_run_id="123456")

    assert verified.manifest.task_id == _task().task_id


def test_oe_upgrade_candidate_requires_only_declared_architecture(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundle

    task = _oe_task()
    _payload(tmp_path)
    (tmp_path / "reports/aarch64.json").unlink()
    (tmp_path / "reports/aarch64.junit.xml").unlink()
    native = json.loads((tmp_path / "reports/x86_64.json").read_text())
    native["task_key"] = task.task_key
    native["checks"]["os_identity"] = True
    (tmp_path / "reports/x86_64.json").write_text(json.dumps(native))
    results_path = tmp_path / "reports/results.json"
    results = json.loads(results_path.read_text())
    results["task_id"] = task.task_id
    results["task_key"] = task.task_key
    results["architectures"] = {"x86_64": results["architectures"]["x86_64"]}
    results["architectures"]["x86_64"]["checks"]["os_identity"] = True
    results_path.write_text(json.dumps(results))
    (tmp_path / "reports/agents/derivation-report.json").write_text(
        '{"schema_version":1}\n'
    )

    bundle = CandidateBundle.create(
        tmp_path,
        task=task,
        base_sha=BASE_SHA,
        validated_run_id="123456",
        request_key="a" * 16,
    )

    assert bundle.manifest.task_key == task.task_key
    assert bundle.manifest.architectures == ("x86_64",)
    assert CandidateBundle.verify(tmp_path).task == task


def test_oe_upgrade_manifest_records_activity_and_derivation_evidence(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundle

    task = _oe_task()
    _payload(tmp_path)
    (tmp_path / "reports/aarch64.json").unlink()
    (tmp_path / "reports/aarch64.junit.xml").unlink()
    native = json.loads((tmp_path / "reports/x86_64.json").read_text())
    native["task_key"] = task.task_key
    native["checks"]["os_identity"] = True
    (tmp_path / "reports/x86_64.json").write_text(json.dumps(native))
    results_path = tmp_path / "reports/results.json"
    results = json.loads(results_path.read_text())
    results["task_id"] = task.task_id
    results["task_key"] = task.task_key
    results["architectures"] = {"x86_64": results["architectures"]["x86_64"]}
    results["architectures"]["x86_64"]["checks"]["os_identity"] = True
    results_path.write_text(json.dumps(results))
    (tmp_path / "reports/agents/derivation-report.json").write_text(
        '{"schema_version":1}\n'
    )
    (tmp_path / "reports/agents/sanitization-round1.json").write_text(
        '{"schema_version":1,"clean":true}\n'
    )

    bundle = CandidateBundle.create(
        tmp_path,
        task=task,
        base_sha=BASE_SHA,
        validated_run_id="123456",
        request_key="a" * 16,
    )

    assert bundle.manifest.scenario == "oe-upgrade"
    assert bundle.manifest.request_key == "a" * 16
    assert bundle.manifest.mdu_path == "Database/redis"
    assert bundle.manifest.derive_from == "8.2.1/24.03-lts-sp1"
    assert bundle.manifest.derivation_report_sha256.startswith("sha256:")
    assert bundle.manifest.sanitization_reports == (
        "reports/agents/sanitization-round1.json",
    )


def test_oe_upgrade_candidate_requires_derivation_audit_evidence(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundle, CandidateBundleError

    task = _oe_task()
    _payload(tmp_path)
    (tmp_path / "reports/aarch64.json").unlink()
    (tmp_path / "reports/aarch64.junit.xml").unlink()
    native = json.loads((tmp_path / "reports/x86_64.json").read_text())
    native["task_key"] = task.task_key
    native["checks"]["os_identity"] = True
    (tmp_path / "reports/x86_64.json").write_text(json.dumps(native))
    results_path = tmp_path / "reports/results.json"
    results = json.loads(results_path.read_text())
    results["task_id"] = task.task_id
    results["task_key"] = task.task_key
    results["architectures"] = {"x86_64": results["architectures"]["x86_64"]}
    results["architectures"]["x86_64"]["checks"]["os_identity"] = True
    results_path.write_text(json.dumps(results))

    with pytest.raises(CandidateBundleError, match="derivation report"):
        CandidateBundle.create(
            tmp_path,
            task=task,
            base_sha=BASE_SHA,
            validated_run_id="123456",
            request_key="a" * 16,
        )


def test_candidate_bundle_rejects_modified_file(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundle, CandidateBundleError

    _payload(tmp_path)
    CandidateBundle.create(
        tmp_path,
        task=_task(),
        base_sha=BASE_SHA,
        validated_run_id="123456",
    )
    (tmp_path / "changes.patch").write_text("tampered")

    with pytest.raises(CandidateBundleError, match="checksum"):
        CandidateBundle.verify(tmp_path, expected_run_id="123456")


def test_candidate_bundle_rejects_unlisted_file(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundle, CandidateBundleError

    _payload(tmp_path)
    CandidateBundle.create(
        tmp_path,
        task=_task(),
        base_sha=BASE_SHA,
        validated_run_id="123456",
    )
    (tmp_path / "unexpected.txt").write_text("not in manifest")

    with pytest.raises(CandidateBundleError, match="file set"):
        CandidateBundle.verify(tmp_path, expected_run_id="123456")


def test_candidate_bundle_requires_both_architectures_and_gates(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundle, CandidateBundleError

    _payload(tmp_path)
    (tmp_path / "reports" / "aarch64.json").unlink()

    with pytest.raises(CandidateBundleError, match="aarch64"):
        CandidateBundle.create(
            tmp_path,
            task=_task(),
            base_sha=BASE_SHA,
            validated_run_id="123456",
        )


def test_candidate_bundle_requires_hadolint_evidence_even_when_clean(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundle, CandidateBundleError

    _payload(tmp_path)
    (tmp_path / "reports" / "hadolint.txt").unlink()

    with pytest.raises(CandidateBundleError, match="hadolint"):
        CandidateBundle.create(
            tmp_path,
            task=_task(),
            base_sha=BASE_SHA,
            validated_run_id="123456",
        )


def test_candidate_bundle_requires_internal_schema_v1_results(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundle, CandidateBundleError

    _payload(tmp_path)
    (tmp_path / "reports" / "results.json").unlink()

    with pytest.raises(CandidateBundleError, match="results.json"):
        CandidateBundle.create(
            tmp_path,
            task=_task(),
            base_sha=BASE_SHA,
            validated_run_id="123456",
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("schema_version", 2, "schema"),
        ("task_id", "another-task", "task"),
        ("validated_run_id", "999999", "run ID"),
    ),
)
def test_candidate_bundle_validates_internal_results_identity(
    tmp_path, field, value, message
):
    from scripts.lib.candidate_bundle import CandidateBundle, CandidateBundleError

    _payload(tmp_path)
    path = tmp_path / "reports" / "results.json"
    payload = json.loads(path.read_text())
    payload[field] = value
    path.write_text(json.dumps(payload))

    with pytest.raises(CandidateBundleError, match=message):
        CandidateBundle.create(
            tmp_path,
            task=_task(),
            base_sha=BASE_SHA,
            validated_run_id="123456",
        )


@pytest.mark.parametrize(
    "junit",
    (
        '<testsuite tests="1" failures="1" errors="0"/>',
        '<testsuite tests="1" failures="-1" errors="1"/>',
        '<testsuite tests="0" failures="0" errors="0"/>',
        '<testsuite tests="1" failures="0" errors="0" skipped="1"/>',
    ),
)
def test_candidate_bundle_requires_passing_junit_evidence(tmp_path, junit):
    from scripts.lib.candidate_bundle import CandidateBundle, CandidateBundleError

    _payload(tmp_path)
    (tmp_path / "reports" / "aarch64.junit.xml").write_text(junit)

    with pytest.raises(CandidateBundleError, match="aarch64 JUnit"):
        CandidateBundle.create(
            tmp_path,
            task=_task(),
            base_sha=BASE_SHA,
            validated_run_id="123456",
        )


def test_candidate_bundle_requires_passed_reports(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundle, CandidateBundleError

    _payload(tmp_path)
    (tmp_path / "reports" / "x86_64.json").write_text('{"status":"failed"}\n')

    with pytest.raises(CandidateBundleError, match="x86_64"):
        CandidateBundle.create(
            tmp_path,
            task=_task(),
            base_sha=BASE_SHA,
            validated_run_id="123456",
        )


def test_candidate_bundle_rejects_generation_delivery_stop(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundle, CandidateBundleError

    _payload(tmp_path)
    (tmp_path / "reports" / "generation-gates.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "delivery_allowed": False,
                "findings": [
                    {
                        "code": "agent.identity_decision",
                        "level": "delivery_stop",
                    }
                ],
            }
        )
    )

    with pytest.raises(CandidateBundleError, match="generation.*delivery"):
        CandidateBundle.create(
            tmp_path,
            task=_task(),
            base_sha=BASE_SHA,
            validated_run_id="123456",
        )


def test_candidate_bundle_rejects_empty_patch(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundle, CandidateBundleError

    _payload(tmp_path)
    (tmp_path / "changes.patch").write_text("")

    with pytest.raises(CandidateBundleError, match="changes.patch"):
        CandidateBundle.create(
            tmp_path,
            task=_task(),
            base_sha=BASE_SHA,
            validated_run_id="123456",
        )


def test_candidate_bundle_rejects_run_id_mismatch(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundle, CandidateBundleError

    _payload(tmp_path)
    CandidateBundle.create(
        tmp_path,
        task=_task(),
        base_sha=BASE_SHA,
        validated_run_id="123456",
    )

    with pytest.raises(CandidateBundleError, match="run ID"):
        CandidateBundle.verify(tmp_path, expected_run_id="999999")


def test_candidate_bundle_rejects_symlink_payload(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundle, CandidateBundleError

    _payload(tmp_path)
    (tmp_path / "reports" / "external.json").symlink_to(tmp_path / "changes.patch")

    with pytest.raises(CandidateBundleError, match="symlink"):
        CandidateBundle.create(
            tmp_path,
            task=_task(),
            base_sha=BASE_SHA,
            validated_run_id="123456",
        )
