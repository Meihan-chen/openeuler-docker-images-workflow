def _task(name, version="1.0.0"):
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


def _request():
    from scripts.lib.oe_upgrade_contract import UpgradeRequest

    return UpgradeRequest.create(
        tracking_issue_number=19,
        oe_version="26.03-lts",
        scope=("Database",),
        base_sha="1" * 40,
    )


def test_resolver_follows_terminal_priority_and_selects_first_pending():
    from scripts.lib.oe_upgrade_advance import (
        PullRequestView,
        RunView,
        resolve_advance,
    )

    tasks = tuple(_task(name) for name in ("a", "b", "c", "d", "e"))
    prs = (
        PullRequestView(
            number=8,
            url="https://gitcode.example/pulls/8",
            title="x",
            body=f"<!-- oe-upgrade-task:{tasks[1].task_key} -->",
            head=tasks[1].branch,
            base="master",
            state="open",
            merged=False,
        ),
    )
    runs = (
        RunView(
            run_id="11",
            task_key=tasks[3].task_key,
            status="completed",
            conclusion="failure",
            url="https://github.example/actions/runs/11",
        ),
    )

    result = resolve_advance(
        request=_request(),
        tasks=tasks,
        base_targets={tasks[0].task_key: "exists"},
        head_targets={},
        pull_requests=prs,
        failure_reasons={tasks[2].task_key: "build"},
        runs=runs,
    )

    assert [state.status for state in result.states] == [
        "skipped-existing",
        "pr-created",
        "failed",
        "failed",
        "pending",
    ]
    assert result.next_task == tasks[4]
    assert result.fallback_failures == ((tasks[3].task_key, "infrastructure"),)


def test_active_worker_blocks_dispatch_even_when_another_task_is_pending():
    from scripts.lib.oe_upgrade_advance import RunView, resolve_advance

    first, second = _task("a"), _task("b")
    result = resolve_advance(
        request=_request(),
        tasks=(first, second),
        base_targets={},
        head_targets={},
        pull_requests=(),
        failure_reasons={},
        runs=(
            RunView(
                run_id="22",
                task_key=first.task_key,
                status="in_progress",
                conclusion="",
                url="https://github.example/actions/runs/22",
            ),
        ),
    )

    assert result.action == "active-worker-exists"
    assert result.next_task is None
    assert [state.status for state in result.states] == ["running", "pending"]


def test_head_target_distinguishes_merged_from_unattributed_satisfaction():
    from scripts.lib.oe_upgrade_advance import PullRequestView, resolve_advance

    merged, external = _task("a"), _task("b")
    result = resolve_advance(
        request=_request(),
        tasks=(merged, external),
        base_targets={},
        head_targets={merged.task_key: "exists", external.task_key: "exists"},
        pull_requests=(
            PullRequestView(
                number=9,
                url="https://gitcode.example/pulls/9",
                title="x",
                body=f"<!-- oe-upgrade-task:{merged.task_key} -->",
                head=merged.branch,
                base="master",
                state="closed",
                merged=True,
            ),
        ),
        failure_reasons={},
        runs=(),
    )

    assert [state.status for state in result.states] == [
        "merged",
        "satisfied-after-base",
    ]
    assert result.action == "finalize"


def test_inconsistent_target_is_a_contract_failure_not_existing():
    from scripts.lib.oe_upgrade_advance import resolve_advance

    task = _task("redis")
    result = resolve_advance(
        request=_request(),
        tasks=(task,),
        base_targets={task.task_key: "inconsistent"},
        head_targets={},
        pull_requests=(),
        failure_reasons={},
        runs=(),
    )

    assert result.states[0].status == "failed"
    assert result.states[0].reason == "contract"
    assert result.fallback_failures == ((task.task_key, "contract"),)


def test_github_run_name_is_parsed_without_one_request_per_task():
    from scripts.lib.oe_upgrade_advance import parse_workflow_runs

    request = _request()
    task = _task("redis")
    title = (
        f"oe-upgrade / {request.request_key} / {task.task_key} / "
        f"{task.mdu_path}"
    )

    runs = parse_workflow_runs(
        [
            {
                "databaseId": 123,
                "displayTitle": title,
                "status": "completed",
                "conclusion": "failure",
                "url": "https://github.example/actions/runs/123",
            },
            {
                "databaseId": 999,
                "displayTitle": "another workflow run",
                "status": "completed",
                "conclusion": "success",
                "url": "https://github.example/actions/runs/999",
            },
        ],
        request_key=request.request_key,
    )

    assert len(runs) == 1
    assert runs[0].task_key == task.task_key


def test_github_runs_are_listed_once_with_token_only_in_environment():
    from scripts.lib.oe_upgrade_advance import list_github_workflow_runs

    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return type(
            "Completed",
            (),
            {
                "stdout": (
                    '[{"databaseId":123,"displayTitle":"x",'
                    '"status":"completed","conclusion":"success",'
                    '"url":"https://github.example/actions/runs/123"}]'
                )
            },
        )()

    rows = list_github_workflow_runs(
        github_token="secret",
        github_repository="example/workflow",
        workflow="oe_upgrade_worker.yml",
        run=run,
    )

    assert len(rows) == 1
    command, kwargs = calls[0]
    assert command[:3] == ["gh", "run", "list"]
    assert "secret" not in command
    assert kwargs["env"]["GH_TOKEN"] == "secret"


def test_target_state_index_requires_matching_file_meta_and_architectures(tmp_path):
    import subprocess

    from scripts.lib.oe_upgrade_advance import target_state_index

    task = _task("redis")
    mdu = tmp_path / "Database" / "redis"
    target = mdu / task.version / task.os_version
    target.mkdir(parents=True)
    (target / "Dockerfile").write_text("FROM scratch\n")
    (mdu / "meta.yml").write_text(
        "1.0.0-oe2603lts:\n"
        "  path: 1.0.0/26.03-lts/Dockerfile\n"
    )
    subprocess.run(["git", "init", "-b", "master"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=tmp_path, check=True)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert target_state_index(tmp_path, revision, (task,)) == {
        task.task_key: "exists"
    }

    (mdu / "meta.yml").write_text("{}\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "break meta"], cwd=tmp_path, check=True)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert target_state_index(tmp_path, revision, (task,)) == {
        task.task_key: "inconsistent"
    }
