from dataclasses import dataclass

import pytest


TARGET_REPO = "openeuler/openeuler-docker-images"


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


@dataclass(frozen=True)
class Resource:
    number: int
    url: str


class StatefulIssueClient:
    def __init__(self, existing=None):
        self.issue = existing
        self.calls = []

    def list_issues(self, **kwargs):
        self.calls.append(("list", kwargs))
        return [self.issue] if self.issue else []

    def create_issue(self, **kwargs):
        self.calls.append(("create", kwargs))
        self.issue = {
            "number": 19,
            "html_url": "https://gitcode.com/example/issues/19",
            "title": kwargs["title"],
            "body": kwargs["body"],
            "state": "open",
        }
        return Resource(number=19, url=self.issue["html_url"])

    def update_issue(self, **kwargs):
        self.calls.append(("update", kwargs))
        self.issue = {
            "number": kwargs["number"],
            "html_url": "https://gitcode.com/example/issues/19",
            "title": kwargs["title"],
            "body": kwargs["body"],
            "state": kwargs["state"],
        }
        return Resource(number=kwargs["number"], url=self.issue["html_url"])

    def create_issue_comment(self, **kwargs):
        self.calls.append(("comment", kwargs))
        return {"id": 3, "body": kwargs["body"]}


def _report(*, attempts=3, terminal_delivery_error=False):
    from scripts.lib.issue_lifecycle import FailureIssueReport

    return FailureIssueReport(
        task=_task(),
        failure_stage="dual-arch-test",
        attempt_count=attempts,
        terminal_delivery_error=terminal_delivery_error,
        architecture_status={
            "x86_64": "passed",
            "aarch64": "failed: version assertion",
        },
        repair_summaries=(
            "round 1: corrected build dependency",
            "round 2: corrected ARM library path",
            "round 3: version assertion still fails",
        ),
        run_url="https://github.com/Meihan-chen/repo/actions/runs/123456",
        artifact_url="https://github.com/Meihan-chen/repo/actions/runs/123456",
        suggested_action="Inspect the aarch64 version output.",
    )


@pytest.mark.parametrize(
    ("successful", "attempts"),
    [(True, 3), (False, 1), (False, 2)],
)
def test_intermediate_failure_or_success_never_writes_issue(successful, attempts):
    from scripts.lib.issue_lifecycle import report_terminal_failure

    client = StatefulIssueClient()

    result = report_terminal_failure(
        client=client,
        target_repo=TARGET_REPO,
        report=_report(attempts=attempts),
        successful=successful,
    )

    assert result is None
    assert client.calls == []


def test_terminal_failure_creates_structured_needs_human_review_issue():
    from scripts.lib.issue_lifecycle import report_terminal_failure

    client = StatefulIssueClient()

    result = report_terminal_failure(
        client=client,
        target_repo=TARGET_REPO,
        report=_report(),
        successful=False,
    )

    assert result.number == 19
    assert [name for name, _ in client.calls] == ["list", "create"]
    list_call = client.calls[0][1]
    assert list_call == {
        "target_repo": TARGET_REPO,
        "state": "all",
        "search": _task().task_id,
    }
    create_call = client.calls[1][1]
    assert create_call["labels"] == "needs-human-review"
    assert not create_call["title"].startswith("[E2E TEST]")
    body = create_call["body"]
    assert f"<!-- oe-autopilot-task:{_task().task_id} -->" in body
    assert _task().to_json() in body
    assert "dual-arch-test" in body
    assert "x86_64: passed" in body
    assert "aarch64: failed: version assertion" in body
    assert "round 3: version assertion still fails" in body
    assert "Inspect the aarch64 version output." in body


def test_terminal_delivery_error_can_report_before_third_repair_round():
    from scripts.lib.issue_lifecycle import report_terminal_failure

    client = StatefulIssueClient()

    result = report_terminal_failure(
        client=client,
        target_repo=TARGET_REPO,
        report=_report(attempts=1, terminal_delivery_error=True),
        successful=False,
    )

    assert result.number == 19
    assert [name for name, _ in client.calls] == ["list", "create"]


def test_existing_issue_with_exact_marker_is_updated_not_duplicated():
    from scripts.lib.issue_lifecycle import report_terminal_failure

    marker = f"<!-- oe-autopilot-task:{_task().task_id} -->"
    client = StatefulIssueClient(
        existing={
            "number": "27",
            "html_url": "https://gitcode.com/example/issues/27",
            "title": "old",
            "body": f"{marker}\nold failure",
            "state": "closed",
        }
    )

    result = report_terminal_failure(
        client=client,
        target_repo=TARGET_REPO,
        report=_report(),
        successful=False,
    )

    assert result.number == 27
    assert [name for name, _ in client.calls] == ["list", "update"]
    update_call = client.calls[1][1]
    assert update_call["number"] == 27
    assert update_call["state"] == "open"
    assert update_call["labels"] == "needs-human-review"


def test_controlled_probe_is_explicit_idempotent_commented_and_closed():
    from scripts.lib.issue_lifecycle import run_controlled_issue_probe

    client = StatefulIssueClient()

    result = run_controlled_issue_probe(
        client=client,
        target_repo=TARGET_REPO,
        task=_task(),
        environment="test",
        operation="failure_issue_contract_test",
        github_run_id="123456",
        failure_stage="aarch64-build",
    )

    assert result.number == 19
    assert result.url.endswith("/issues/19")
    assert [name for name, _ in client.calls] == [
        "list",
        "create",
        "list",
        "update",
        "comment",
        "update",
    ]
    create_call = client.calls[1][1]
    assert create_call["title"].startswith("[E2E TEST]")
    assert "Kvrocks 2.16.0" in create_call["title"]
    assert "aarch64-build" in create_call["title"]
    assert "run 123456" in create_call["title"]
    assert "e2e-" in create_call["body"]
    assert client.calls[-1][1]["state"] == "closed"
    assert "closed automatically" in client.calls[-1][1]["body"]


def test_controlled_probe_never_creates_twice_when_new_issue_is_not_yet_listed():
    from scripts.lib.issue_lifecycle import (
        IssueLifecycleError,
        run_controlled_issue_probe,
    )

    class DelayedIndexClient(StatefulIssueClient):
        def list_issues(self, **kwargs):
            self.calls.append(("list", kwargs))
            return []

    client = DelayedIndexClient()

    with pytest.raises(IssueLifecycleError, match="not visible"):
        run_controlled_issue_probe(
            client=client,
            target_repo=TARGET_REPO,
            task=_task(),
            environment="test",
            operation="failure_issue_contract_test",
            github_run_id="123456",
            failure_stage="aarch64-build",
        )

    assert [name for name, _ in client.calls] == [
        "list",
        "create",
        "list",
    ]


@pytest.mark.parametrize(
    ("environment", "operation"),
    [
        ("production", "failure_issue_contract_test"),
        ("test", "validate_only"),
        ("test", "fork_pr"),
    ],
)
def test_controlled_probe_rejects_non_explicit_or_non_test_invocation(
    environment, operation
):
    from scripts.lib.issue_lifecycle import (
        IssueOperationForbiddenError,
        run_controlled_issue_probe,
    )

    client = StatefulIssueClient()

    with pytest.raises(IssueOperationForbiddenError):
        run_controlled_issue_probe(
            client=client,
            target_repo=TARGET_REPO,
            task=_task(),
            environment=environment,
            operation=operation,
            github_run_id="123456",
            failure_stage="aarch64-build",
        )

    assert client.calls == []
