import json
from dataclasses import dataclass

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


def _candidate(
    tmp_path,
    *,
    testcase_qa_status="approved",
    testcase_repaired=False,
):
    from scripts.lib.candidate_bundle import CandidateBundle

    root = tmp_path / "candidate"
    (root / "reports" / "agents").mkdir(parents=True)
    (root / "changes.patch").write_text(
        "diff --git a/Database/image-list.yml "
        "b/Database/image-list.yml\n"
        "index 1111111..2222222 100644\n"
        "--- a/Database/image-list.yml\n"
        "+++ b/Database/image-list.yml\n"
        "@@ -1 +1,2 @@\n"
        "+  kvrocks: kvrocks\n"
        "diff --git "
        "a/Database/kvrocks/2.16.0/24.03-lts-sp4/Dockerfile "
        "b/Database/kvrocks/2.16.0/24.03-lts-sp4/Dockerfile\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/Database/kvrocks/2.16.0/24.03-lts-sp4/Dockerfile\n"
        "@@ -0,0 +1 @@\n"
        "+FROM openeuler/openeuler:24.03-lts-sp4\n"
    )
    for architecture in ("x86_64", "aarch64"):
        (root / "reports" / f"{architecture}.json").write_text(
            json.dumps(
                {
                    "status": "passed",
                    "architecture": architecture,
                    "platform": (
                        "linux/amd64"
                        if architecture == "x86_64"
                        else "linux/arm64"
                    ),
                    "image_id": f"sha256:{architecture}",
                    "duration_seconds": 12,
                    "checks": {
                        "native_build": True,
                        "dgoss": True,
                        "shared_tests": True,
                        "restart_persistence": True,
                    },
                }
            )
        )
    (root / "reports" / "gates.json").write_text(
        json.dumps({"status": "passed", "added_files": 14})
    )
    (root / "reports" / "agents" / "image-qa-round1.json").write_text(
        json.dumps(
            {
                "status": "approved",
                "issues": [],
                "summary": "Image metadata and runtime contract are consistent.",
            }
        )
    )
    if testcase_repaired or testcase_qa_status == "needs_fix":
        (root / "reports" / "agents" / "testcase-qa-round1.json").write_text(
            json.dumps(
                {
                    "status": "needs_fix",
                    "issues": [
                        {
                            "severity": "major",
                            "file": "Database/kvrocks/tests/test.sh",
                            "description": "Version assertion is too weak.",
                        }
                    ],
                    "coverage_score": 0.75,
                    "summary": "The version check can produce a false positive.",
                }
            )
        )
    testcase_round = (
        2 if testcase_repaired or testcase_qa_status == "needs_fix" else 1
    )
    testcase_issues = (
        [{"severity": "major", "description": "coverage gap"}]
        if testcase_qa_status == "needs_fix"
        else []
    )
    testcase_report = (
        root
        / "reports"
        / "agents"
        / f"testcase-qa-round{testcase_round}.json"
    )
    testcase_report.write_text(
        json.dumps(
            {
                "status": testcase_qa_status,
                "issues": testcase_issues,
                "coverage_score": 0.95,
                "summary": (
                    "The repaired tests now verify the exact application version."
                    if testcase_repaired
                    else testcase_qa_status
                ),
            }
        )
    )
    return CandidateBundle.create(
        root,
        task=_task(),
        base_sha="1" * 40,
        validated_run_id="123456",
    )


def _config(mode="fork_pr"):
    from scripts.lib.gitcode_client import DeliveryConfig

    return DeliveryConfig.from_mapping(
        {
            "environment": "test",
            "delivery_mode": mode,
            "target_repo": "openeuler/openeuler-docker-images",
            "push_repo": "qq_42020325/openeuler-docker-images",
            "target_branch": "master",
        }
    )


@dataclass(frozen=True)
class Promotion:
    branch: str = "auto/new-image/kvrocks/2.16.0-oe2403sp4"
    commit_sha: str = "2" * 40
    base_sha: str = "1" * 40
    candidate_sha256: str = "3" * 64
    validated_run_id: str = "123456"


@dataclass(frozen=True)
class Resource:
    number: int = 88
    url: str = (
        "https://gitcode.com/openeuler/openeuler-docker-images/pull/88"
    )


class PRClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def create_pull_request(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return Resource()


def test_pr_content_contains_candidate_and_dual_architecture_evidence(tmp_path):
    from scripts.harness.compose_pr import compose_pull_request

    bundle = _candidate(tmp_path)

    content = compose_pull_request(bundle)

    assert content.title == (
        "[New Image] Add kvrocks 2.16.0 for openEuler 24.03-lts-sp4"
    )
    assert [line for line in content.body.splitlines() if line.startswith("## ")] == [
        "## Summary",
        "## Changes",
        "## Adversarial review",
        "## Repository checks",
        "## Checklist",
    ]
    assert "| Modified | `Database/image-list.yml` |" in content.body
    assert (
        "| Added | "
        "`Database/kvrocks/2.16.0/24.03-lts-sp4/Dockerfile` |"
    ) in content.body
    assert "x86_64" in content.body
    assert "aarch64" in content.body
    assert "restart_persistence" in content.body
    assert bundle.manifest.content_sha256 in content.body
    assert "validated run `123456`" in content.body
    assert "Final result: `approved` after 1 round." in content.body
    assert "Image metadata and runtime contract are consistent." in content.body
    assert "Result evidence: `results.json`, `version_info.json`" in content.body
    assert "https://github.com/apache/kvrocks/tree/v2.16.0" in content.body
    assert "Confidence Score" not in content.body
    assert "Apache Kvrocks" not in content.body
    assert "Redis-protocol" not in content.body
    assert "`Database/image-list.yml`" not in content.body.split(
        "## Repository checks", 1
    )[1]


def test_pr_content_summarizes_qa_rounds_and_findings(tmp_path):
    from scripts.harness.compose_pr import compose_pull_request

    bundle = _candidate(tmp_path, testcase_repaired=True)

    content = compose_pull_request(bundle)

    assert "### Testcase review" in content.body
    assert "Final result: `approved` after 2 rounds." in content.body
    assert "Round 1: `needs_fix`" in content.body
    assert "The version check can produce a false positive." in content.body
    assert (
        "`major` — `Database/kvrocks/tests/test.sh`: "
        "Version assertion is too weak."
    ) in content.body
    assert "Round 2: `approved`" in content.body
    assert "The repaired tests now verify the exact application version." in (
        content.body
    )


def test_pr_content_records_non_blocking_qa_disagreement(tmp_path):
    from scripts.harness.compose_pr import compose_pull_request

    bundle = _candidate(tmp_path, testcase_qa_status="needs_fix")

    content = compose_pull_request(bundle)

    assert "### Image review" in content.body
    assert "Final result: `approved` after 1 round." in content.body
    assert "### Testcase review" in content.body
    assert "Final result: `needs_fix` after 2 rounds." in content.body
    assert "`major` — coverage gap" in content.body


def test_pr_content_records_recovered_candidate_provenance(tmp_path):
    bundle = _candidate(tmp_path)
    (bundle.root / "reports" / "recovery-provenance.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "recover_package",
                "status": "passed",
                "generation_run_id": "30478803960",
                "validation_run_id": "30483501656",
                "packaging_run_id": "123456",
                "validated_base_sha": "2" * 40,
                "current_base_sha": "1" * 40,
                "upstream_changed_paths": [
                    "Security/reports/docker.io/openeuler/nginx/latest.md"
                ],
                "candidate_changed_paths": [
                    "Database/kvrocks/README.md"
                ],
                "promotable": True,
            }
        )
    )
    from scripts.harness.compose_pr import compose_pull_request

    content = compose_pull_request(bundle)

    assert "- Generation evidence run: `30478803960`." in content.body
    assert "- Native validation evidence run: `30483501656`." in content.body
    assert "- Recovery packaging run: `123456`." in content.body
    assert "Promoted from validated run `123456`" not in content.body
    assert "- Packaged in recovery run: `123456`." in content.body
    assert "Non-overlapping target-base advance: verified." in content.body


def test_pr_content_rejects_recovery_packaging_run_mismatch(tmp_path):
    from scripts.harness.compose_pr import PRDeliveryError, compose_pull_request

    bundle = _candidate(tmp_path)
    (bundle.root / "reports" / "recovery-provenance.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "recover_package",
                "status": "passed",
                "generation_run_id": "30478803960",
                "validation_run_id": "30483501656",
                "packaging_run_id": "999999",
                "validated_base_sha": "2" * 40,
                "current_base_sha": "1" * 40,
                "upstream_changed_paths": ["Security/scan.md"],
                "candidate_changed_paths": ["Database/kvrocks/README.md"],
                "promotable": True,
            }
        )
    )

    with pytest.raises(PRDeliveryError, match="packaging run"):
        compose_pull_request(bundle)


def test_delivery_pushes_then_creates_cross_repository_pr(tmp_path):
    from scripts.harness.compose_pr import deliver_promoted_candidate

    calls = []
    client = PRClient()

    result = deliver_promoted_candidate(
        repo=tmp_path,
        config=_config(),
        promotion=Promotion(),
        username="qq_42020325",
        token="secret",
        title="title",
        body="body",
        client=client,
        push=lambda **kwargs: calls.append(("push", kwargs)),
        delete=lambda **kwargs: calls.append(("delete", kwargs)),
    )

    assert result.number == 88
    assert [name for name, _ in calls] == ["push"]
    assert client.calls == [
        {
            "config": _config(),
            "title": "title",
            "body": "body",
            "branch": Promotion().branch,
        }
    ]


def test_pr_api_failure_deletes_exact_pushed_branch(tmp_path):
    from scripts.harness.compose_pr import deliver_promoted_candidate
    from scripts.utils.gitcode import GitCodeAPIError

    calls = []
    client = PRClient(GitCodeAPIError("create failed"))

    with pytest.raises(GitCodeAPIError, match="create failed"):
        deliver_promoted_candidate(
            repo=tmp_path,
            config=_config(),
            promotion=Promotion(),
            username="qq_42020325",
            token="secret",
            title="title",
            body="body",
            client=client,
            push=lambda **kwargs: calls.append(("push", kwargs)),
            delete=lambda **kwargs: calls.append(("delete", kwargs)),
        )

    assert [name for name, _ in calls] == ["push", "delete"]
    assert calls[1][1]["branch"] == Promotion().branch


def test_validate_only_refuses_before_push_or_pr(tmp_path):
    from scripts.harness.compose_pr import PRDeliveryError, deliver_promoted_candidate

    calls = []
    client = PRClient()

    with pytest.raises(PRDeliveryError, match="forbids"):
        deliver_promoted_candidate(
            repo=tmp_path,
            config=_config("validate_only"),
            promotion=Promotion(),
            username="qq_42020325",
            token="secret",
            title="title",
            body="body",
            client=client,
            push=lambda **kwargs: calls.append(("push", kwargs)),
            delete=lambda **kwargs: calls.append(("delete", kwargs)),
        )

    assert calls == []
    assert client.calls == []
