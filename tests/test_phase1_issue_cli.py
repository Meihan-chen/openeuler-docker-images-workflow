import argparse
import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest


def _task_file(tmp_path):
    path = tmp_path / "task.json"
    path.write_text(
        json.dumps(
            {
                "app": "kvrocks",
                "version": "2.16.0",
                "os_version": "24.03-lts-sp4",
                "domain": "Database",
                "source_url": (
                    "https://github.com/apache/kvrocks/tree/v2.16.0"
                ),
                "scenario": "new-image",
            }
        )
    )
    return path


@dataclass(frozen=True)
class Resource:
    number: int = 19
    url: str = "https://gitcode.com/example/issues/19"


def test_issue_contract_handler_is_fixed_to_explicit_test_operation(
    tmp_path, monkeypatch, capsys
):
    from scripts.harness import flow

    calls = []
    client = object()
    monkeypatch.setenv("GITCODE_TOKEN", "secret")
    monkeypatch.setattr(
        flow,
        "GitCodeClient",
        lambda **kwargs: calls.append(("client", kwargs)) or client,
    )
    monkeypatch.setattr(
        flow,
        "run_controlled_issue_probe",
        lambda **kwargs: calls.append(("probe", kwargs)) or Resource(),
    )

    flow._issue_contract_test(
        argparse.Namespace(
            task_spec=_task_file(tmp_path),
            github_run_id="123456",
            failure_stage="aarch64-build",
        )
    )

    assert calls[0] == ("client", {"token": "secret"})
    probe = calls[1][1]
    assert probe["client"] is client
    assert probe["target_repo"] == "openeuler/openeuler-docker-images"
    assert probe["environment"] == "test"
    assert probe["operation"] == "failure_issue_contract_test"
    assert probe["github_run_id"] == "123456"
    assert json.loads(capsys.readouterr().out) == {
        "number": 19,
        "state": "closed",
        "url": "https://gitcode.com/example/issues/19",
    }


def test_issue_contract_handler_requires_token_before_client_or_probe(
    tmp_path, monkeypatch
):
    from scripts.harness import flow
    from scripts.lib.issue_lifecycle import IssueLifecycleError

    calls = []
    monkeypatch.delenv("GITCODE_TOKEN", raising=False)
    monkeypatch.setattr(
        flow,
        "GitCodeClient",
        lambda **kwargs: calls.append("client"),
    )
    monkeypatch.setattr(
        flow,
        "run_controlled_issue_probe",
        lambda **kwargs: calls.append("probe"),
    )

    with pytest.raises(IssueLifecycleError, match="GITCODE_TOKEN"):
        flow._issue_contract_test(
            argparse.Namespace(
                task_spec=_task_file(tmp_path),
                github_run_id="123456",
                failure_stage="aarch64-build",
            )
        )

    assert calls == []


def test_issue_watch_handler_claims_and_dispatches_canonical_workflow(
    monkeypatch, capsys
):
    from scripts.harness import flow

    calls = []
    client = object()
    claimed = SimpleNamespace(number=64, url="https://gitcode.com/issues/64")
    monkeypatch.setenv("GITCODE_TOKEN", "gitcode-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setattr(
        flow,
        "GitCodeClient",
        lambda **kwargs: calls.append(("client", kwargs)) or client,
    )
    monkeypatch.setattr(
        flow,
        "dispatch_github_workflow",
        lambda **kwargs: calls.append(("dispatch", kwargs)),
    )

    def claim(**kwargs):
        calls.append(("claim", kwargs))
        kwargs["dispatch"]({"operation": "scenario_one"})
        return claimed

    monkeypatch.setattr(flow, "claim_new_image_issue", claim)

    flow._issue_watch(
        argparse.Namespace(
            target_repo="openeuler/openeuler-docker-images",
            issue_number=64,
            github_repository="Meihan-chen/openeuler-docker-images-workflow",
            github_ref="main",
            workflow="new-image.yml",
        )
    )

    assert calls[0] == ("client", {"token": "gitcode-secret"})
    assert calls[1][0] == "claim"
    assert calls[1][1]["client"] is client
    dispatch = calls[2][1]
    assert dispatch["github_token"] == "github-secret"
    assert dispatch["workflow"] == "new-image.yml"
    assert dispatch["inputs"] == {"operation": "scenario_one"}
    assert json.loads(capsys.readouterr().out) == {
        "dispatched": True,
        "issue_number": 64,
        "issue_url": "https://gitcode.com/issues/64",
    }


def test_issue_finalize_handler_updates_the_exact_source_issue(
    monkeypatch, capsys
):
    from scripts.harness import flow

    calls = []
    client = object()
    monkeypatch.setenv("GITCODE_TOKEN", "gitcode-secret")
    monkeypatch.setattr(
        flow,
        "GitCodeClient",
        lambda **kwargs: client,
    )
    monkeypatch.setattr(
        flow,
        "finalize_new_image_issue",
        lambda **kwargs: calls.append(kwargs),
    )

    flow._issue_finalize(
        argparse.Namespace(
            target_repo="openeuler/openeuler-docker-images",
            issue_number=64,
            outcome="success",
            run_url="https://github.com/example/repo/actions/runs/123",
            pr_url="https://gitcode.com/example/repo/pull/9",
            failure_summary="",
        )
    )

    assert calls == [
        {
            "client": client,
            "target_repo": "openeuler/openeuler-docker-images",
            "issue_number": 64,
            "outcome": "success",
            "run_url": "https://github.com/example/repo/actions/runs/123",
            "pr_url": "https://gitcode.com/example/repo/pull/9",
            "failure_summary": "",
        }
    ]
    assert json.loads(capsys.readouterr().out) == {
        "issue_number": 64,
        "outcome": "success",
    }


def test_issue_trigger_commands_exist_on_the_shared_flow_cli():
    from scripts.harness import flow

    watch = flow._parser().parse_args(
        [
            "issue-watch",
            "--target-repo",
            "openeuler/openeuler-docker-images",
            "--issue-number",
            "64",
            "--github-repository",
            "Meihan-chen/openeuler-docker-images-workflow",
            "--github-ref",
            "main",
        ]
    )
    finalize = flow._parser().parse_args(
        [
            "issue-finalize",
            "--target-repo",
            "openeuler/openeuler-docker-images",
            "--issue-number",
            "64",
            "--outcome",
            "failure",
            "--run-url",
            "https://github.com/example/repo/actions/runs/123",
        ]
    )

    assert watch.handler is flow._issue_watch
    assert finalize.handler is flow._issue_finalize


def test_issue_watch_number_filter_is_optional_for_production_selection():
    from scripts.harness import flow

    watch = flow._parser().parse_args(
        [
            "issue-watch",
            "--target-repo",
            "openeuler/openeuler-docker-images",
            "--github-repository",
            "opensourceways/openeuler-docker-autopilot",
            "--github-ref",
            "main",
        ]
    )

    assert watch.issue_number is None
