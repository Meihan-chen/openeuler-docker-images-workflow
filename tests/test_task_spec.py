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
    if scenario == "oe-upgrade":
        raw.update(
            {
                "schema_version": 2,
                "source_url": "",
                "image_name": "kvrocks",
                "mdu_path": "Database/kvrocks",
                "derive_from": "2.16.0/24.03-lts-sp4",
                "architectures": ["x86_64", "aarch64"],
            }
        )
    task = TaskSpec.from_workflow_dispatch(raw)

    assert task.scenario == scenario
    assert TaskSpec.from_json(task.to_json()).scenario == scenario


def test_oe_upgrade_task_spec_v2_supports_nested_mdu_and_stable_identity():
    from scripts.lib.task_spec import TaskSpec

    task = TaskSpec.from_workflow_dispatch(
        {
            "schema_version": 2,
            "scenario": "oe-upgrade",
            "app": "agent",
            "image_name": "kserve-agent",
            "version": "0.15.2",
            "os_version": "26.03-LTS",
            "domain": "AI",
            "source_url": "",
            "mdu_path": "AI/kserve/agent",
            "derive_from": "0.15.2/24.03-lts",
            "architectures": ["x86_64"],
        }
    )

    assert task.schema_version == 2
    assert task.mdu_path == "AI/kserve/agent"
    assert task.architectures == ("x86_64",)
    assert task.task_key == "83022a491afdf5cc"
    assert task.task_id == "oe-upgrade-83022a491afdf5cc"
    assert task.branch == (
        "auto/oe-upgrade/kserve-agent-4d10a832/0.15.2-oe2603lts"
    )
    assert TaskSpec.from_json(task.to_json()) == task


def test_oe_upgrade_task_key_is_recomputed_instead_of_trusted():
    from scripts.lib.task_spec import TaskSpec, TaskSpecError

    with pytest.raises(TaskSpecError, match="task_key"):
        TaskSpec.from_workflow_dispatch(
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
                "derive_from": "8.2.1/24.03-lts-sp1",
                "architectures": ["x86_64", "aarch64"],
                "task_key": "forged",
            }
        )


@pytest.mark.parametrize(
    "raw",
    (
        {**_kvrocks_input(), "scenario": "oe-upgrade"},
        {
            **_kvrocks_input(),
            "schema_version": 2,
            "scenario": "new-image",
            "mdu_path": "Database/kvrocks",
        },
    ),
)
def test_task_spec_rejects_schema_scenario_mismatch(raw):
    from scripts.lib.task_spec import TaskSpec, TaskSpecError

    with pytest.raises(TaskSpecError, match="schema_version"):
        TaskSpec.from_workflow_dispatch(raw)


@pytest.mark.parametrize(
    "field,value",
    (
        ("mdu_path", "/Database/redis"),
        ("mdu_path", "Database/../Others/redis"),
        ("derive_from", "../8.2.1/24.03-lts-sp1"),
        ("architectures", ["linux/amd64"]),
    ),
)
def test_oe_upgrade_task_spec_rejects_unsafe_v2_fields(field, value):
    from scripts.lib.task_spec import TaskSpec, TaskSpecError

    raw = {
        "schema_version": 2,
        "scenario": "oe-upgrade",
        "app": "redis",
        "image_name": "redis",
        "version": "8.2.1",
        "os_version": "26.03-lts",
        "domain": "Database",
        "source_url": "",
        "mdu_path": "Database/redis",
        "derive_from": "8.2.1/24.03-lts-sp1",
        "architectures": ["x86_64", "aarch64"],
    }
    raw[field] = value

    with pytest.raises(TaskSpecError, match=field):
        TaskSpec.from_workflow_dispatch(raw)


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
