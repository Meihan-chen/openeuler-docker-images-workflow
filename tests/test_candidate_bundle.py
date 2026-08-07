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


def _payload(root: Path):
    (root / "reports" / "agents").mkdir(parents=True)
    (root / "changes.patch").write_text("diff --git a/a b/a\n")
    report = {
        "status": "passed",
        "checks": {
            "native_build": True,
            "dgoss": True,
            "shared_tests": True,
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
