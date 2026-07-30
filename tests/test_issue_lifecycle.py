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


MINIMAL_ISSUE_TITLE = (
    "【new-image】add kvrocks 2.16.0 docker image on "
    "openEuler 24.03-LTS-SP4"
)
MINIMAL_ISSUE_BODY = """**软件包名称（Package Name）：** kvrocks
**源码仓库（Source Repository）：** https://github.com/apache/kvrocks/tree/v2.16.0
**所属领域（Domain）：** 数据库
"""


class TriggerIssueClient:
    def __init__(self, issue):
        self.issue = dict(issue)
        self.calls = []

    def get_issue(self, **kwargs):
        self.calls.append(("get", kwargs))
        return dict(self.issue)

    def update_issue(self, **kwargs):
        self.calls.append(("update", kwargs))
        self.issue.update(
            {
                "title": kwargs["title"],
                "body": kwargs["body"],
                "state": kwargs["state"],
                "issue_state": kwargs.get("issue_status"),
                "issue_state_detail": {
                    "title": kwargs.get("issue_status"),
                },
            }
        )
        return Resource(
            number=kwargs["number"],
            url=f"https://gitcode.com/example/issues/{kwargs['number']}",
        )

    def create_issue_comment(self, **kwargs):
        self.calls.append(("comment", kwargs))
        return {"id": 3, "body": kwargs["body"]}


class ScanningIssueClient(TriggerIssueClient):
    def list_issues(self, **kwargs):
        self.calls.append(("list", kwargs))
        return [
            _new_trigger_issue(
                number=63,
                title="ordinary request",
                issue_state="新建",
                issue_state_detail={"title": "新建"},
            ),
            dict(self.issue),
        ]


def _new_trigger_issue(**overrides):
    issue = {
        "number": 64,
        "html_url": "https://gitcode.com/example/issues/64",
        "title": MINIMAL_ISSUE_TITLE,
        "body": MINIMAL_ISSUE_BODY,
        "state": "open",
        "issue_state": "新建",
        "issue_state_detail": {"title": "新建"},
    }
    issue.update(overrides)
    return issue


def _parse_trigger_task(issue):
    from scripts.harness.parse_issue import parse_issue_request
    from scripts.lib.task_spec import TaskSpec

    fields = parse_issue_request(issue["title"], issue["body"])
    return TaskSpec.from_workflow_dispatch(
        {
            "app": fields["package_name"],
            "version": fields["app_version"],
            "os_version": fields["os_version"],
            "domain": fields["domain"],
            "source_url": fields["source_repo_url"],
        }
    )


def test_new_issue_is_claimed_before_dispatch_and_cannot_dispatch_twice():
    from scripts.lib.issue_lifecycle import claim_new_image_issue

    client = TriggerIssueClient(_new_trigger_issue())
    dispatched = []

    claimed = claim_new_image_issue(
        client=client,
        target_repo=TARGET_REPO,
        issue_number=64,
        dispatch=dispatched.append,
        parse_task=_parse_trigger_task,
    )
    duplicate = claim_new_image_issue(
        client=client,
        target_repo=TARGET_REPO,
        issue_number=64,
        dispatch=dispatched.append,
        parse_task=_parse_trigger_task,
    )

    assert claimed is not None
    assert claimed.number == 64
    assert claimed.task == _task()
    assert duplicate is None
    assert len(dispatched) == 1
    assert dispatched[0] == {
        "operation": "scenario_one",
        "app": "kvrocks",
        "version": "2.16.0",
        "os_version": "24.03-lts-sp4",
        "domain": "Database",
        "source_url": "https://github.com/apache/kvrocks/tree/v2.16.0",
        "source_run_id": "issue:64",
    }
    assert [name for name, _ in client.calls[:4]] == [
        "get",
        "update",
        "comment",
        "get",
    ]
    claim_call = client.calls[1][1]
    assert claim_call["issue_status"] == "已接纳"
    assert claim_call["state"] == "open"


def test_production_selection_uses_new_status_and_new_image_title():
    from scripts.lib.issue_lifecycle import claim_new_image_issue

    client = ScanningIssueClient(_new_trigger_issue())
    dispatched = []

    result = claim_new_image_issue(
        client=client,
        target_repo=TARGET_REPO,
        issue_number=None,
        dispatch=dispatched.append,
        parse_task=_parse_trigger_task,
    )

    assert result is not None
    assert result.number == 64
    assert client.calls[0] == (
        "list",
        {
            "target_repo": TARGET_REPO,
            "state": "open",
            "search": "【new-image】",
        },
    )
    assert len(dispatched) == 1


def test_invalid_new_issue_is_rejected_without_dispatch():
    from scripts.lib.issue_lifecycle import claim_new_image_issue

    client = TriggerIssueClient(
        _new_trigger_issue(
            body=MINIMAL_ISSUE_BODY.replace(
                "https://github.com/apache/kvrocks/tree/v2.16.0",
                "",
            )
        )
    )
    dispatched = []

    result = claim_new_image_issue(
        client=client,
        target_repo=TARGET_REPO,
        issue_number=64,
        dispatch=dispatched.append,
        parse_task=_parse_trigger_task,
    )

    assert result is None
    assert dispatched == []
    assert [name for name, _ in client.calls] == ["get", "comment", "update"]
    reject_call = client.calls[-1][1]
    assert reject_call["issue_status"] == "已拒绝"
    assert reject_call["state"] == "closed"


def test_dispatch_failure_returns_claimed_issue_to_new_state():
    from scripts.lib.issue_lifecycle import (
        IssueLifecycleError,
        claim_new_image_issue,
    )

    client = TriggerIssueClient(_new_trigger_issue())

    def fail_dispatch(_):
        raise RuntimeError("GitHub unavailable")

    with pytest.raises(IssueLifecycleError, match="dispatch"):
        claim_new_image_issue(
            client=client,
            target_repo=TARGET_REPO,
            issue_number=64,
            dispatch=fail_dispatch,
            parse_task=_parse_trigger_task,
        )

    assert client.issue["issue_state"] == "新建"
    assert [name for name, _ in client.calls] == ["get", "update", "update"]


@pytest.mark.parametrize(
    ("outcome", "pr_url", "expected_status", "expected_state"),
    [
        (
            "success",
            "https://gitcode.com/openeuler/openeuler-docker-images/pull/4000",
            "已完成",
            "closed",
        ),
        ("failure", "", "已挂起", "open"),
    ],
)
def test_trigger_issue_is_finalized_from_scenario_one_result(
    outcome, pr_url, expected_status, expected_state
):
    from scripts.lib.issue_lifecycle import finalize_new_image_issue

    client = TriggerIssueClient(
        _new_trigger_issue(
            issue_state="已接纳",
            issue_state_detail={"title": "已接纳"},
        )
    )

    finalize_new_image_issue(
        client=client,
        target_repo=TARGET_REPO,
        issue_number=64,
        outcome=outcome,
        run_url="https://github.com/Meihan-chen/repo/actions/runs/123",
        pr_url=pr_url,
        failure_summary="package_candidate=failure",
    )

    assert [name for name, _ in client.calls] == ["get", "comment", "update"]
    comment = client.calls[1][1]["body"]
    assert "actions/runs/123" in comment
    if outcome == "success":
        assert pr_url in comment
    else:
        assert "package_candidate=failure" in comment
    update = client.calls[2][1]
    assert update["issue_status"] == expected_status
    assert update["state"] == expected_state


def test_github_workflow_dispatch_uses_token_only_in_environment():
    from scripts.lib.issue_lifecycle import dispatch_github_workflow

    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))

    dispatch_github_workflow(
        github_token="github-secret",
        github_repository="Meihan-chen/openeuler-docker-images-workflow",
        workflow="new-image.yml",
        ref="main",
        inputs={"operation": "scenario_one", "source_run_id": "64"},
        run=run,
    )

    command, kwargs = calls[0]
    assert command[:7] == [
        "gh",
        "workflow",
        "run",
        "new-image.yml",
        "--repo",
        "Meihan-chen/openeuler-docker-images-workflow",
        "--ref",
    ]
    assert "github-secret" not in command
    assert kwargs["env"]["GH_TOKEN"] == "github-secret"
    assert kwargs["check"] is True
