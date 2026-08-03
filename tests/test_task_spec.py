import json

import pytest


def _kvrocks_input():
    return {
        "app": "Kvrocks",
        "version": "2.16.0",
        "os_version": "24.03-LTS-SP4",
        "domain": "database",
        "source_url": "https://github.com/apache/kvrocks",
    }


def test_task_spec_normalizes_workflow_dispatch_input():
    from scripts.lib.task_spec import TaskSpec

    task = TaskSpec.from_workflow_dispatch(_kvrocks_input())

    assert task.app == "kvrocks"
    assert task.os_version == "24.03-lts-sp4"
    assert task.domain == "Database"
    assert task.scenario == "new-image"


def test_task_spec_derives_stable_identifiers():
    from scripts.lib.task_spec import TaskSpec

    task = TaskSpec.from_workflow_dispatch(_kvrocks_input())

    assert task.task_id == "new-image-database-kvrocks-2.16.0-24.03-lts-sp4"
    assert task.branch == "auto/new-image/kvrocks/2.16.0-oe2403sp4"


def test_task_spec_json_round_trip_is_stable():
    from scripts.lib.task_spec import TaskSpec

    task = TaskSpec.from_workflow_dispatch(_kvrocks_input())
    encoded = task.to_json()

    assert TaskSpec.from_json(encoded) == task
    assert encoded == json.dumps(task.to_dict(), ensure_ascii=False, sort_keys=True)


@pytest.mark.parametrize("scenario", ["new-image", "version-update", "oe-upgrade"])
def test_task_spec_preserves_each_supported_scenario(scenario):
    from scripts.lib.task_spec import TaskSpec

    raw = {**_kvrocks_input(), "scenario": scenario}
    task = TaskSpec.from_workflow_dispatch(raw)

    assert task.scenario == scenario
    assert TaskSpec.from_json(task.to_json()).scenario == scenario


def test_task_spec_rejects_unknown_scenario():
    from scripts.lib.task_spec import TaskSpec, TaskSpecError

    with pytest.raises(TaskSpecError, match="scenario"):
        TaskSpec.from_workflow_dispatch(
            {**_kvrocks_input(), "scenario": "surprise-mode"}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("app", "../kvrocks"),
        ("version", "2.16.0/../../main"),
        ("os_version", "24.03 lts sp4"),
        ("domain", "Unknown"),
        ("source_url", "http://github.com/apache/kvrocks"),
    ],
)
def test_task_spec_rejects_unsafe_input(field, value):
    from scripts.lib.task_spec import TaskSpec, TaskSpecError

    raw = _kvrocks_input()
    raw[field] = value

    with pytest.raises(TaskSpecError, match=field):
        TaskSpec.from_workflow_dispatch(raw)


def test_task_spec_rejects_missing_input():
    from scripts.lib.task_spec import TaskSpec, TaskSpecError

    raw = _kvrocks_input()
    del raw["version"]

    with pytest.raises(TaskSpecError, match="version"):
        TaskSpec.from_workflow_dispatch(raw)


@pytest.mark.parametrize(
    "source_url",
    (
        "https://gitee.com/openeuler/community/tree/master",
        "https://gitee.com/src-openeuler/redis/tree/master",
    ),
)
def test_task_spec_rejects_migrated_openeuler_gitee_source(source_url):
    from scripts.lib.task_spec import TaskSpec, TaskSpecError

    raw = _kvrocks_input()
    raw["source_url"] = source_url

    with pytest.raises(TaskSpecError, match="source_url.*gitcode"):
        TaskSpec.from_workflow_dispatch(raw)


def test_task_spec_accepts_third_party_gitee_source():
    from scripts.lib.task_spec import TaskSpec

    raw = _kvrocks_input()
    raw["source_url"] = "https://gitee.com/example/kvrocks/tree/v2.16.0"

    assert TaskSpec.from_workflow_dispatch(raw).source_url == raw["source_url"]
