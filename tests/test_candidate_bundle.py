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
    (root / "reports").mkdir(parents=True)
    (root / "changes.patch").write_text("diff --git a/a b/a\n")
    (root / "reports" / "x86_64.json").write_text('{"status":"passed"}\n')
    (root / "reports" / "aarch64.json").write_text('{"status":"passed"}\n')
    (root / "reports" / "gates.json").write_text('{"status":"passed"}\n')


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
        "reports/gates.json",
        "reports/x86_64.json",
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


def test_candidate_bundle_marks_changed_base_for_revalidation(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundle

    _payload(tmp_path)
    bundle = CandidateBundle.create(
        tmp_path,
        task=_task(),
        base_sha=BASE_SHA,
        validated_run_id="123456",
    )

    assert bundle.promotion_action(BASE_SHA) == "reuse"
    assert bundle.promotion_action("a" * 40) == "revalidate"


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
