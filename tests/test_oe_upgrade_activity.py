import json

import pytest


def _request():
    from scripts.lib.oe_upgrade_contract import UpgradeRequest

    return UpgradeRequest.create(
        tracking_issue_number=19,
        oe_version="26.03-lts",
        scope=("Database",),
        base_sha="1" * 40,
    )


def _task(name="redis", version="8.2.1"):
    from scripts.lib.task_spec import TaskSpec

    return TaskSpec.from_workflow_dispatch(
        {
            "schema_version": 2,
            "scenario": "oe-upgrade",
            "app": name,
            "image_name": name,
            "version": version,
            "os_version": "26.03-lts",
            "domain": "Database",
            "source_url": "",
            "mdu_path": f"Database/{name}",
            "derive_from": f"{version}/24.03-lts-sp4",
            "architectures": ["x86_64", "aarch64"],
        }
    )


def test_request_comment_round_trips_only_from_the_trusted_bot():
    from scripts.lib.oe_upgrade_activity import (
        parse_request_comment,
        render_request_comment,
    )

    request = _request()
    body = render_request_comment(request)
    comments = [
        {"body": body, "user": {"login": "attacker"}},
        {"body": body, "user": {"login": "oe-bot"}},
    ]

    assert parse_request_comment(comments, trusted_author="oe-bot") == request
    assert f"oe-upgrade-request:v1:{request.request_key}" in body
    assert json.dumps(request.to_dict(), ensure_ascii=False, sort_keys=True) in body


def test_duplicate_or_conflicting_request_markers_fail_closed():
    from scripts.lib.oe_upgrade_activity import (
        ActivityError,
        parse_request_comment,
        render_request_comment,
    )
    from scripts.lib.oe_upgrade_contract import UpgradeRequest

    first = _request()
    second = UpgradeRequest.create(
        tracking_issue_number=19,
        oe_version="27.03-lts",
        scope=("Database",),
        base_sha="2" * 40,
    )

    with pytest.raises(ActivityError, match="multiple upgrade requests"):
        parse_request_comment(
            [
                {"body": render_request_comment(first), "user": {"login": "oe-bot"}},
                {"body": render_request_comment(second), "user": {"login": "oe-bot"}},
            ],
            trusted_author="oe-bot",
        )


def test_failure_comment_is_idempotent_after_uncertain_post():
    from scripts.lib.oe_upgrade_activity import ensure_issue_comment, failure_marker

    marker = failure_marker(_request().request_key, _task().task_key)

    class Client:
        def __init__(self):
            self.comments = []
            self.posts = 0

        def list_issue_comments(self, **_):
            return list(self.comments)

        def create_issue_comment(self, **kwargs):
            self.posts += 1
            self.comments.append(
                {"body": kwargs["body"], "user": {"login": "oe-bot"}}
            )
            raise RuntimeError("response lost after POST")

    client = Client()
    created = ensure_issue_comment(
        client=client,
        target_repo="openeuler/openeuler-docker-images",
        issue_number=19,
        body=f"failed\n\n{marker}",
        marker=marker,
        trusted_author="oe-bot",
    )
    repeated = ensure_issue_comment(
        client=client,
        target_repo="openeuler/openeuler-docker-images",
        issue_number=19,
        body=f"failed\n\n{marker}",
        marker=marker,
        trusted_author="oe-bot",
    )

    assert created is True
    assert repeated is False
    assert client.posts == 1


def test_rejection_comment_has_a_stable_marker_and_bounded_reason():
    from scripts.lib.oe_upgrade_activity import (
        rejection_marker,
        render_rejection_comment,
    )

    marker = rejection_marker(
        19,
        "【oe-upgrade】bad title",
        "**Target openEuler Version**: nope",
    )
    body = render_rejection_comment(
        issue_number=19,
        title="【oe-upgrade】bad title",
        issue_body="**Target openEuler Version**: nope",
        reason="invalid " + "x" * 3000,
    )

    assert marker in body
    assert "请求格式校验失败" in body
    assert len(body) < 1800


def test_reject_issue_request_comments_once_and_sets_rejected_status():
    from scripts.lib.oe_upgrade_activity import reject_issue_request

    class Client:
        def __init__(self):
            self.comments = []
            self.updates = []

        def list_issue_comments(self, **_):
            return list(self.comments)

        def create_issue_comment(self, **kwargs):
            self.comments.append(
                {"body": kwargs["body"], "user": {"login": "oe-bot"}}
            )

        def update_issue(self, **kwargs):
            self.updates.append(kwargs)

    client = Client()
    issue = {
        "number": 19,
        "title": "【oe-upgrade】invalid",
        "body": "bad",
    }
    reject_issue_request(
        client=client,
        target_repo="openeuler/openeuler-docker-images",
        issue=issue,
        reason="body field missing",
        trusted_author="oe-bot",
    )
    reject_issue_request(
        client=client,
        target_repo="openeuler/openeuler-docker-images",
        issue=issue,
        reason="body field missing",
        trusted_author="oe-bot",
    )

    assert len(client.comments) == 1
    assert [update["issue_status"] for update in client.updates] == [
        "已拒绝",
        "已拒绝",
    ]


def test_worker_result_validates_success_and_failure_shapes():
    from scripts.lib.oe_upgrade_activity import WorkerResult

    task = _task()
    success = WorkerResult.create(
        request_key=_request().request_key,
        task=task,
        outcome="pr-created",
        reason="",
        run_id="123",
        run_url="https://github.example/actions/runs/123",
        pr_number=7,
        pr_url="https://gitcode.example/pulls/7",
        candidate_digest="sha256:" + "a" * 64,
    )
    assert WorkerResult.from_mapping(success.to_dict()) == success

    with pytest.raises(ValueError, match="reason"):
        WorkerResult.create(
            request_key=_request().request_key,
            task=task,
            outcome="failed",
            reason="",
            run_id="123",
            run_url="https://github.example/actions/runs/123",
        )


def test_stable_state_digest_changes_only_with_business_state():
    from scripts.lib.oe_upgrade_activity import ResolvedTaskState, state_digest

    task = _task()
    state = ResolvedTaskState(
        schema_version=1,
        request_key=_request().request_key,
        task_key=task.task_key,
        mdu_path=task.mdu_path,
        status="pr-created",
        reason="",
        evidence_source="open-pr",
        run_id="123",
        pr_number=7,
        pr_url="https://gitcode.example/pulls/7",
    )

    assert state_digest((state,), ()) == state_digest((state,), ())
    changed = ResolvedTaskState(**{**state.to_dict(), "status": "merged"})
    assert state_digest((state,), ()) != state_digest((changed,), ())


def test_failure_and_summary_comments_are_bounded_and_machine_readable():
    from scripts.lib.oe_upgrade_activity import (
        ResolvedTaskState,
        render_failure_comment,
        render_summary_comment,
    )

    request = _request()
    task = _task()
    failure = render_failure_comment(
        request=request,
        task=task,
        reason="build",
        run_url="https://github.example/actions/runs/123",
        summary="docker build failed\n" + "x" * 5000,
        artifact_name="oe-upgrade-worker-123",
    )
    assert f"oe-upgrade-failure:{request.request_key}:{task.task_key}" in failure
    assert "Reason: `build`" in failure
    assert len(failure) < 3000

    state = ResolvedTaskState(
        schema_version=1,
        request_key=request.request_key,
        task_key=task.task_key,
        mdu_path=task.mdu_path,
        status="failed",
        reason="build",
        evidence_source="failure-marker",
        run_id="123",
        pr_number=None,
        pr_url="",
    )
    summary = render_summary_comment(
        request=request,
        states=(state,),
        planning_failures=(),
        run_url="https://github.example/actions/runs/999",
        artifact_name="oe-upgrade-final-999",
    )
    assert "failed: `1`" in summary
    assert "oe-upgrade-summary:" in summary


def test_failure_markers_are_parsed_only_from_trusted_comments():
    from scripts.lib.oe_upgrade_activity import parse_failure_reasons

    request = _request()
    task = _task()
    marker = (
        f"<!-- oe-upgrade-failure:{request.request_key}:{task.task_key} -->"
    )
    comments = [
        {"body": f"Reason: `build`\n{marker}", "user": {"login": "attacker"}},
        {
            "body": f"Reason: `runtime-test`\n{marker}",
            "user": {"login": "oe-bot"},
        },
    ]

    assert parse_failure_reasons(
        comments,
        request_key=request.request_key,
        trusted_author="oe-bot",
    ) == {task.task_key: "runtime-test"}


def test_deliver_entry_establishes_one_request_but_plan_is_read_only(tmp_path):
    from scripts.lib.oe_upgrade_activity import establish_request

    request = _request()

    class Client:
        def __init__(self):
            self.comments = []
            self.updates = []

        def list_issue_comments(self, **_):
            return self.comments

        def create_issue_comment(self, **kwargs):
            self.comments.append(
                {"body": kwargs["body"], "user": {"login": "oe-bot"}}
            )

        def update_issue(self, **kwargs):
            self.updates.append(kwargs)

    issue = {"number": 19, "title": "upgrade", "body": "body", "state": "open"}
    plan_client = Client()
    establish_request(
        client=plan_client,
        target_repo="openeuler/openeuler-docker-images",
        issue=issue,
        request=request,
        mode="plan",
        trusted_author="oe-bot",
    )
    assert plan_client.comments == []
    assert plan_client.updates == []

    deliver_client = Client()
    establish_request(
        client=deliver_client,
        target_repo="openeuler/openeuler-docker-images",
        issue=issue,
        request=request,
        mode="deliver",
        trusted_author="oe-bot",
    )
    assert len(deliver_client.comments) == 1
    assert deliver_client.updates[0]["issue_status"] == "已接纳"
