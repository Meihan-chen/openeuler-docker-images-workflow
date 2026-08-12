import subprocess


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "target"
    for name in ("a", "b"):
        mdu = repo / "Database" / name
        source = mdu / "1.0.0" / "24.03-lts-sp4"
        source.mkdir(parents=True)
        (source / "Dockerfile").write_text(
            "FROM openeuler/openeuler:24.03-lts-sp4\n"
        )
        (mdu / "meta.yml").write_text(
            "1.0.0-oe2403sp4:\n"
            "  path: 1.0.0/24.03-lts-sp4/Dockerfile\n"
        )
    (repo / "Database" / "image-list.yml").write_text(
        "images:\n  a: a\n  b: b\n"
    )
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo


def _issue():
    return {
        "number": 19,
        "title": (
            "【oe-upgrade】upgrade latest application images to "
            "openEuler 26.03-LTS"
        ),
        "body": (
            "**openEuler 目标版本（Target openEuler Version）：** "
            "26.03-lts\n\n**Scope：** Database\n"
        ),
        "state": "open",
        "issue_state_detail": {"title": "新建"},
    }


class Client:
    def __init__(self):
        self.comments = []
        self.updates = []
        self.pulls = []

    def list_issue_comments(self, **_):
        return list(self.comments)

    def create_issue_comment(self, **kwargs):
        self.comments.append(
            {"body": kwargs["body"], "user": {"login": "oe-bot"}}
        )

    def update_issue(self, **kwargs):
        self.updates.append(kwargs)

    def list_pull_requests(self, **_):
        return list(self.pulls)


def test_plan_only_writes_preview_and_never_establishes_or_dispatches(tmp_path):
    from scripts.lib.oe_upgrade_controller import run_activity

    client = Client()
    dispatched = []
    result = run_activity(
        client=client,
        target_repo="openeuler/openeuler-docker-images",
        issue=_issue(),
        workspace=_repo(tmp_path),
        mode="plan",
        expected_oe_version="26.03-lts",
        expected_scope="Database",
        trusted_author="oe-bot",
        run_url="https://github.example/actions/runs/1",
        artifact_name="oe-upgrade-plan-1",
        runs=(),
        dispatch=lambda **kwargs: dispatched.append(kwargs),
    )

    assert result.action == "planned"
    assert len(result.plan.tasks) == 2
    assert dispatched == []
    assert client.updates == []
    assert "oe-upgrade-request:" not in client.comments[0]["body"]


def test_deliver_establishes_request_and_dispatches_only_first_task(tmp_path):
    from scripts.lib.oe_upgrade_controller import run_activity

    client = Client()
    dispatched = []
    result = run_activity(
        client=client,
        target_repo="openeuler/openeuler-docker-images",
        issue=_issue(),
        workspace=_repo(tmp_path),
        mode="deliver",
        expected_oe_version="26.03-lts",
        expected_scope="Database",
        trusted_author="oe-bot",
        run_url="https://github.example/actions/runs/1",
        artifact_name="oe-upgrade-plan-1",
        runs=(),
        dispatch=lambda **kwargs: dispatched.append(kwargs),
    )

    assert result.action == "dispatched"
    assert result.next_task.mdu_path == "Database/a"
    assert len(dispatched) == 1
    assert dispatched[0]["task"].task_key == result.next_task.task_key
    assert client.updates[0]["issue_status"] == "已接纳"
    assert any("oe-upgrade-request:" in item["body"] for item in client.comments)


def test_completed_failure_is_commented_then_next_task_is_dispatched(tmp_path):
    from scripts.lib.oe_upgrade_activity import render_request_comment
    from scripts.lib.oe_upgrade_advance import RunView
    from scripts.lib.oe_upgrade_contract import UpgradeRequest
    from scripts.lib.oe_upgrade_controller import run_activity
    from scripts.lib.oe_upgrade_planner import plan_upgrade

    repo = _repo(tmp_path)
    request = UpgradeRequest.create(
        tracking_issue_number=19,
        oe_version="26.03-lts",
        scope=("Database",),
        base_sha=_git(repo, "rev-parse", "HEAD"),
    )
    first = plan_upgrade(repo, request).tasks[0]
    client = Client()
    client.comments.append(
        {"body": render_request_comment(request), "user": {"login": "oe-bot"}}
    )
    dispatched = []

    result = run_activity(
        client=client,
        target_repo="openeuler/openeuler-docker-images",
        issue={**_issue(), "issue_state_detail": {"title": "已接纳"}},
        workspace=repo,
        mode="deliver",
        expected_oe_version="26.03-lts",
        expected_scope="Database",
        trusted_author="oe-bot",
        run_url="https://github.example/actions/runs/2",
        artifact_name="oe-upgrade-plan-2",
        runs=(
            RunView(
                run_id="7",
                task_key=first.task_key,
                status="completed",
                conclusion="failure",
                url="https://github.example/actions/runs/7",
            ),
        ),
        dispatch=lambda **kwargs: dispatched.append(kwargs),
    )

    assert result.action == "dispatched"
    assert result.next_task.mdu_path == "Database/b"
    assert any("Reason: `infrastructure`" in item["body"] for item in client.comments)


def test_all_terminal_tasks_write_one_summary_and_suspend_issue(tmp_path):
    from scripts.lib.oe_upgrade_activity import render_request_comment, task_marker
    from scripts.lib.oe_upgrade_contract import UpgradeRequest
    from scripts.lib.oe_upgrade_controller import run_activity
    from scripts.lib.oe_upgrade_planner import plan_upgrade

    repo = _repo(tmp_path)
    request = UpgradeRequest.create(
        tracking_issue_number=19,
        oe_version="26.03-lts",
        scope=("Database",),
        base_sha=_git(repo, "rev-parse", "HEAD"),
    )
    tasks = plan_upgrade(repo, request).tasks
    client = Client()
    client.comments.append(
        {"body": render_request_comment(request), "user": {"login": "oe-bot"}}
    )
    client.pulls = [
        {
            "number": index,
            "url": f"https://gitcode.example/pulls/{index}",
            "title": "upgrade",
            "body": task_marker(task.task_key),
            "head": task.branch,
            "base": "master",
            "state": "open",
            "merged": False,
        }
        for index, task in enumerate(tasks, start=1)
    ]

    result = run_activity(
        client=client,
        target_repo="openeuler/openeuler-docker-images",
        issue={**_issue(), "issue_state_detail": {"title": "已接纳"}},
        workspace=repo,
        mode="deliver",
        expected_oe_version="26.03-lts",
        expected_scope="Database",
        trusted_author="oe-bot",
        run_url="https://github.example/actions/runs/3",
        artifact_name="oe-upgrade-final-3",
        runs=(),
        dispatch=lambda **_: (_ for _ in ()).throw(AssertionError("dispatch")),
    )

    assert result.action == "finalized"
    assert client.updates[-1]["issue_status"] == "已挂起"
    assert sum("oe-upgrade-summary:" in item["body"] for item in client.comments) == 1


def test_worker_precheck_replans_task_and_self_retires_for_open_pr(tmp_path):
    from scripts.lib.oe_upgrade_activity import render_request_comment, task_marker
    from scripts.lib.oe_upgrade_contract import UpgradeRequest
    from scripts.lib.oe_upgrade_controller import verify_worker_start
    from scripts.lib.oe_upgrade_planner import plan_upgrade

    repo = _repo(tmp_path)
    request = UpgradeRequest.create(
        tracking_issue_number=19,
        oe_version="26.03-lts",
        scope=("Database",),
        base_sha=_git(repo, "rev-parse", "HEAD"),
    )
    task = plan_upgrade(repo, request).tasks[0]
    client = Client()
    client.comments.append(
        {"body": render_request_comment(request), "user": {"login": "oe-bot"}}
    )
    client.pulls.append(
        {
            "number": 8,
            "url": "https://gitcode.example/pulls/8",
            "title": "upgrade",
            "body": task_marker(task.task_key),
            "head": task.branch,
            "base": "master",
            "state": "open",
            "merged": False,
        }
    )

    result = verify_worker_start(
        client=client,
        target_repo="openeuler/openeuler-docker-images",
        issue_number=19,
        request_key=request.request_key,
        task=task,
        workspace=repo,
        trusted_author="oe-bot",
        head_revision="HEAD",
    )

    assert result.proceed is False
    assert result.reason == "open-pr"
