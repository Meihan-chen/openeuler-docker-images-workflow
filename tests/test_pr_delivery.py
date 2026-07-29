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


def _candidate(tmp_path, *, testcase_qa_status="approved"):
    from scripts.lib.candidate_bundle import CandidateBundle

    root = tmp_path / "candidate"
    (root / "reports" / "agents").mkdir(parents=True)
    (root / "changes.patch").write_text("diff --git a/a b/a\n")
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
        json.dumps({"status": "approved", "issues": [], "summary": "approved"})
    )
    testcase_round = 1 if testcase_qa_status == "approved" else 2
    (
        root
        / "reports"
        / "agents"
        / f"testcase-qa-round{testcase_round}.json"
    ).write_text(
        json.dumps(
            {
                "status": testcase_qa_status,
                "issues": (
                    []
                    if testcase_qa_status == "approved"
                    else [{"severity": "major", "description": "coverage gap"}]
                ),
                "coverage_score": 0.95,
                "summary": testcase_qa_status,
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
    from scripts.lib.delivery_config import DeliveryConfig

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
        "[New Image] Add Apache Kvrocks 2.16.0 for openEuler 24.03-lts-sp4"
    )
    assert "x86_64" in content.body
    assert "aarch64" in content.body
    assert "restart_persistence" in content.body
    assert bundle.manifest.content_sha256 in content.body
    assert "validated run `123456`" in content.body
    assert "image QA: approved" in content.body
    assert "testcase QA: approved" in content.body
    assert "https://github.com/apache/kvrocks/tree/v2.16.0" in content.body


def test_pr_content_records_non_blocking_qa_disagreement(tmp_path):
    from scripts.harness.compose_pr import compose_pull_request

    bundle = _candidate(tmp_path, testcase_qa_status="needs_fix")

    content = compose_pull_request(bundle)

    assert "image QA: approved" in content.body
    assert "testcase QA: needs_fix" in content.body


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
