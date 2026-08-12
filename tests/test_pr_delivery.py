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


def _oe_upgrade_task(*, architectures=("x86_64",)):
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
            "derive_from": "8.2.1/24.03-lts-sp4",
            "architectures": list(architectures),
        }
    )


def _candidate(
    tmp_path,
    *,
    testcase_qa_status="approved",
    testcase_repaired=False,
    native_fixer=False,
    hadolint_output="",
    gate_delivery_allowed=True,
    task=None,
):
    from scripts.lib.candidate_bundle import CandidateBundle

    task = task or _task()
    architectures = (
        task.architectures
        if task.schema_version == 2
        else ("x86_64", "aarch64")
    )
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
    for architecture in architectures:
        checks = {
            "native_build": True,
            "runtime_test": True,
        }
        if task.scenario == "oe-upgrade":
            checks["os_identity"] = True
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
                    "checks": checks,
                    **(
                        {"task_key": task.task_key}
                        if task.schema_version == 2
                        else {}
                    ),
                }
            )
        )
        (root / "reports" / f"{architecture}.junit.xml").write_text(
            '<testsuite tests="1" failures="0" errors="0"/>'
        )
    gate_report = {"status": "passed", "added_files": 14}
    if gate_delivery_allowed is not None:
        gate_report["delivery_allowed"] = gate_delivery_allowed
    (root / "reports" / "gates.json").write_text(json.dumps(gate_report))
    (root / "reports" / "generation-gates.json").write_text(
        json.dumps({"status": "passed", "delivery_allowed": True})
    )
    (root / "reports" / "hadolint.txt").write_text(hadolint_output)
    (root / "reports" / "results.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "passed",
                "task_id": task.task_id,
                "validated_run_id": "123456",
                "artifact_url": "https://example.test/actions/runs/123456",
                "architectures": {
                    architecture: {
                        "checks": {
                            "native_build": True,
                            "runtime_test": True,
                            **(
                                {"os_identity": True}
                                if task.scenario == "oe-upgrade"
                                else {}
                            ),
                        }
                    }
                    for architecture in architectures
                },
                **(
                    {"task_key": task.task_key}
                    if task.schema_version == 2
                    else {}
                ),
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
    if native_fixer:
        nested_agents = root / "reports" / "agents" / "agents"
        nested_agents.mkdir()
        (nested_agents / "fixer-native-dual-round1-attempt1.json").write_text(
            json.dumps(
                {
                    "_input_review": {
                        "kind": "native_validation_failure",
                        "architectures": {
                            "x86_64": {"status": "failed"},
                            "aarch64": {"status": "failed"},
                        },
                    },
                    "changes": [
                        {
                            "file": (
                                "Database/kvrocks/2.16.0/"
                                "24.03-lts-sp4/Dockerfile"
                            ),
                            "change": (
                                "Disabled unavailable static libstdc++ linkage."
                            ),
                        }
                    ],
                    "status": "fixed",
                    "success": True,
                    "summary": (
                        "Adjusted the build flags for both architectures."
                    ),
                },
                sort_keys=True,
            )
        )
    return CandidateBundle.create(
        root,
        task=task,
        base_sha="1" * 40,
        validated_run_id="123456",
        request_key="a" * 16 if task.scenario == "oe-upgrade" else "",
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


def _production_config():
    from scripts.lib.gitcode_client import DeliveryConfig

    return DeliveryConfig.from_mapping(
        {
            "environment": "production",
            "delivery_mode": "direct_branch_pr",
            "target_repo": "openeuler/openeuler-docker-images",
            "push_repo": "openeuler/openeuler-docker-images",
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
        self.link_calls = []

    def create_pull_request(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return Resource()

    def list_pull_requests(self, **kwargs):
        return []

    def link_pull_request_issue(self, **kwargs):
        self.link_calls.append(kwargs)


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
        "## Fixer process",
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
    assert "runtime_test" in content.body
    assert bundle.manifest.content_sha256 in content.body
    assert "validated run `123456`" in content.body
    assert "Final result: `approved` after 1 round." in content.body
    assert "### Image review" not in content.body
    assert (
        "Target evidence: `version_info.json`, `x86_64.junit.xml`, "
        "`aarch64.junit.xml`" in content.body
    )
    assert "Production candidate evidence: `reports/results.json`" in content.body
    assert "https://github.com/apache/kvrocks/tree/v2.16.0" in content.body
    assert "### Confidence Score" in content.body
    assert "`1.0` (`auto-merge`)" in content.body
    assert "Apache Kvrocks" not in content.body
    assert "Redis-protocol" not in content.body
    assert "`Database/image-list.yml`" not in content.body.split(
        "## Repository checks", 1
    )[1]


def test_oe_upgrade_pr_content_uses_task_marker_and_declared_architectures(tmp_path):
    from scripts.harness.compose_pr import compose_pull_request
    from scripts.lib.oe_upgrade_activity import task_marker

    task = _oe_upgrade_task()
    bundle = _candidate(tmp_path, task=task)

    content = compose_pull_request(bundle)

    assert content.title == (
        "[oe-upgrade] Database/redis 8.2.1 for openEuler 26.03-lts"
    )
    assert task_marker(task.task_key) in content.body
    assert "Derived from `8.2.1/24.03-lts-sp4`." in content.body
    assert "x86_64" in content.body
    assert "aarch64" not in content.body
    assert "os_identity" in content.body


def test_pr_content_records_native_fixer_process(tmp_path):
    from scripts.harness.compose_pr import compose_pull_request

    bundle = _candidate(tmp_path, native_fixer=True)

    content = compose_pull_request(bundle)

    assert "## Fixer process" in content.body
    assert "- Native Fixer invocations: `1`." in content.body
    assert (
        "- Round 1, attempt 1: `fixed` — "
        "Adjusted the build flags for both architectures."
    ) in content.body
    assert (
        "  - Trigger: `native_validation_failure`; failed architectures: "
        "`x86_64`, `aarch64`."
    ) in content.body
    assert (
        "  - Change: "
        "`Database/kvrocks/2.16.0/24.03-lts-sp4/Dockerfile` — "
        "Disabled unavailable static libstdc++ linkage."
    ) in content.body
    assert (
        "- Final outcome: the repaired candidate passed sealed native "
        "validation on `x86_64` and `aarch64`."
    ) in content.body


def test_pr_content_records_when_native_fixer_was_not_needed(tmp_path):
    from scripts.harness.compose_pr import compose_pull_request

    content = compose_pull_request(_candidate(tmp_path))

    assert "## Fixer process" in content.body
    assert "- Native Fixer invocations: `0`." in content.body
    assert "- No native repair was required." in content.body


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


def test_pr_content_reports_hadolint_findings_as_advisory(tmp_path):
    from scripts.harness.compose_pr import compose_pull_request

    bundle = _candidate(
        tmp_path,
        hadolint_output=(
            "Dockerfile:8 DL3041 Specify version with yum install\n"
            "Dockerfile:12 DL3002 Last USER should not be root\n"
            "Dockerfile:15 SC2086 Double quote to prevent globbing\n"
        ),
    )

    content = compose_pull_request(bundle)

    assert "Hadolint: `3` advisory findings" in content.body
    assert "DL3041" in content.body
    assert "DL3002" in content.body
    assert "SC2086" in content.body
    assert "`0.94` (`auto-merge`)" in content.body


def test_pr_content_does_not_require_image_qa_evidence(tmp_path):
    from scripts.harness.compose_pr import compose_pull_request

    bundle = _candidate(tmp_path)
    for path in (bundle.root / "reports" / "agents").glob(
        "image-qa-round*.json"
    ):
        path.unlink()

    content = compose_pull_request(bundle)

    assert "### Image review" not in content.body
    assert "### Testcase review" in content.body


def test_pr_content_rejects_missing_testcase_qa_evidence(tmp_path):
    from scripts.harness.compose_pr import PRDeliveryError, compose_pull_request

    bundle = _candidate(tmp_path)
    for path in (bundle.root / "reports" / "agents").glob(
        "testcase-qa-round*.json"
    ):
        path.unlink()

    with pytest.raises(PRDeliveryError, match="QA evidence is required"):
        compose_pull_request(bundle)


def test_pr_content_rejects_an_incomplete_qa_round_chain(tmp_path):
    from scripts.harness.compose_pr import PRDeliveryError, compose_pull_request

    bundle = _candidate(tmp_path, testcase_qa_status="needs_fix")
    (bundle.root / "reports" / "agents" / "testcase-qa-round2.json").unlink()

    with pytest.raises(PRDeliveryError, match="QA round sequence"):
        compose_pull_request(bundle)


def test_pr_content_accepts_normalized_non_actionable_qa_warning(tmp_path):
    from scripts.harness.compose_pr import compose_pull_request

    bundle = _candidate(tmp_path)
    report_path = (
        bundle.root / "reports" / "agents" / "testcase-qa-round1.json"
    )
    report_path.write_text(
        json.dumps(
            {
                "status": "approved",
                "issues": [],
                "summary": "No candidate issue was described.",
                "harness": {
                    "reported_status": "looks_good",
                    "protocol_warnings": [
                        {
                            "field": "status",
                            "reported": "looks_good",
                            "effective": "approved",
                            "message": "QA status was invalid.",
                        }
                    ],
                    "snapshot": {"status": "full", "complete_text": True},
                },
            }
        )
    )

    content = compose_pull_request(bundle)

    assert "Final result: `approved` after 1 round." in content.body
    assert "QA status was invalid." in content.body


def test_pr_content_discloses_partial_qa_snapshot(tmp_path):
    from scripts.harness.compose_pr import compose_pull_request

    bundle = _candidate(tmp_path)
    report_path = (
        bundle.root / "reports" / "agents" / "testcase-qa-round1.json"
    )
    report = json.loads(report_path.read_text())
    report["harness"] = {
        "snapshot": {
            "status": "compacted",
            "complete_text": False,
            "compacted_files": [
                "Database/kvrocks/2.16.0/24.03-lts-sp4/Dockerfile"
            ],
        }
    }
    report_path.write_text(json.dumps(report))

    content = compose_pull_request(bundle)

    assert "Review input: `compacted` (partial)." in content.body
    assert "Dockerfile" in content.body


def test_pr_content_rejects_noncanonical_native_check_set(tmp_path):
    from scripts.harness.compose_pr import PRDeliveryError, compose_pull_request

    bundle = _candidate(tmp_path)
    report_path = bundle.root / "reports" / "x86_64.json"
    report = json.loads(report_path.read_text())
    report["checks"] = {"native_build": True}
    report_path.write_text(json.dumps(report))

    with pytest.raises(PRDeliveryError, match="checks are incomplete"):
        compose_pull_request(bundle)


def test_pr_content_rejects_buildable_but_not_deliverable_gate(tmp_path):
    from scripts.harness.compose_pr import PRDeliveryError, compose_pull_request

    bundle = _candidate(tmp_path)
    gate = bundle.root / "reports" / "gates.json"
    report = json.loads(gate.read_text())
    report["delivery_allowed"] = False
    gate.write_text(json.dumps(report))

    with pytest.raises(PRDeliveryError, match="delivery contract"):
        compose_pull_request(bundle)


def test_pr_content_rejects_gate_without_explicit_delivery_permission(tmp_path):
    from scripts.harness.compose_pr import PRDeliveryError, compose_pull_request

    bundle = _candidate(tmp_path)
    gate = bundle.root / "reports" / "gates.json"
    report = json.loads(gate.read_text())
    report.pop("delivery_allowed")
    gate.write_text(json.dumps(report))

    with pytest.raises(PRDeliveryError, match="delivery contract"):
        compose_pull_request(bundle)


def test_pr_content_records_non_blocking_qa_disagreement(tmp_path):
    from scripts.harness.compose_pr import compose_pull_request

    bundle = _candidate(tmp_path, testcase_qa_status="needs_fix")

    content = compose_pull_request(bundle)

    assert "### Image review" not in content.body
    assert "### Testcase review" in content.body
    assert "Final result: `needs_fix` after 2 rounds." in content.body
    assert "`major` — coverage gap" in content.body


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


def test_production_delivery_reuses_existing_open_pr_before_push(tmp_path):
    from scripts.harness.compose_pr import deliver_promoted_candidate

    class ExistingClient(PRClient):
        def list_pull_requests(self, **kwargs):
            return [
                {
                    "number": 88,
                    "url": Resource().url,
                    "title": "existing",
                    "body": "marker",
                    "head": Promotion().branch,
                    "base": "master",
                    "state": "open",
                    "merged": False,
                }
            ]

    calls = []
    client = ExistingClient()
    result = deliver_promoted_candidate(
        repo=tmp_path,
        config=_production_config(),
        promotion=Promotion(),
        username="openeuler-bot",
        token="secret",
        title="title",
        body="body",
        client=client,
        issue_number=27,
        push=lambda **kwargs: calls.append(("push", kwargs)),
        delete=lambda **kwargs: calls.append(("delete", kwargs)),
    )

    assert result.number == Resource().number
    assert result.url == Resource().url
    assert calls == []
    assert client.link_calls == [
        {
            "target_repo": "openeuler/openeuler-docker-images",
            "pull_number": 88,
            "issue_number": 27,
        }
    ]


def test_production_delivery_recovers_pr_created_after_uncertain_response(tmp_path):
    from scripts.harness.compose_pr import deliver_promoted_candidate
    from scripts.utils.gitcode import GitCodeAPIError

    class UncertainClient(PRClient):
        def __init__(self):
            super().__init__(GitCodeAPIError("response lost"))
            self.list_count = 0

        def list_pull_requests(self, **kwargs):
            self.list_count += 1
            if self.list_count == 1:
                return []
            return [
                {
                    "number": 88,
                    "url": Resource().url,
                    "title": "created despite timeout",
                    "body": "marker",
                    "head": Promotion().branch,
                    "base": "master",
                    "state": "open",
                    "merged": False,
                }
            ]

    calls = []
    client = UncertainClient()
    result = deliver_promoted_candidate(
        repo=tmp_path,
        config=_production_config(),
        promotion=Promotion(),
        username="openeuler-bot",
        token="secret",
        title="title",
        body="body",
        client=client,
        issue_number=27,
        push=lambda **kwargs: calls.append(("push", kwargs)),
        delete=lambda **kwargs: calls.append(("delete", kwargs)),
    )

    assert result.number == 88
    assert [name for name, _ in calls] == ["push"]
    assert client.list_count == 2
    assert client.link_calls == [
        {
            "target_repo": "openeuler/openeuler-docker-images",
            "pull_number": 88,
            "issue_number": 27,
        }
    ]


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
