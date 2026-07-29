import argparse
import json
from dataclasses import dataclass

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
