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


def _candidate(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundle

    root = tmp_path / "candidate"
    agents = root / "reports" / "agents"
    agents.mkdir(parents=True)
    (root / "changes.patch").write_text("diff --git a/a b/a\n")
    for architecture, platform in (
        ("x86_64", "linux/amd64"),
        ("aarch64", "linux/arm64"),
    ):
        (root / "reports" / f"{architecture}.json").write_text(
            json.dumps(
                {
                    "status": "passed",
                    "platform": platform,
                    "image_id": f"sha256:{architecture}",
                    "duration_seconds": 1,
                    "checks": {
                        "native_build": True,
                        "runtime_test": True,
                    },
                }
            )
        )
        (root / "reports" / f"{architecture}.junit.xml").write_text(
            '<testsuite tests="1" failures="0" errors="0"/>'
        )
    (root / "reports" / "gates.json").write_text(
        '{"status":"passed","delivery_allowed":true,"added_files":14}\n'
    )
    (root / "reports" / "generation-gates.json").write_text(
        '{"status":"passed","delivery_allowed":true}\n'
    )
    (root / "reports" / "hadolint.txt").write_text("")
    (root / "reports" / "results.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "passed",
                "task_id": _task().task_id,
                "validated_run_id": "123456",
                "artifact_url": "https://example.test/actions/runs/123456",
                "architectures": {
                    architecture: {
                        "checks": {
                            "native_build": True,
                            "runtime_test": True,
                        }
                    }
                    for architecture in ("x86_64", "aarch64")
                },
            }
        )
    )
    for name in ("testcase-qa",):
        (agents / f"{name}-round1.json").write_text(
            '{"status":"approved"}\n'
        )
    return CandidateBundle.create(
        root,
        task=_task(),
        base_sha="1" * 40,
        validated_run_id="123456",
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


@dataclass(frozen=True)
class Workspace:
    path: object
    base_sha: str = "1" * 40


@dataclass(frozen=True)
class Promotion:
    branch: str = "auto/new-image/kvrocks/2.16.0-oe2403sp4"


@dataclass(frozen=True)
class Resource:
    number: int = 88
    url: str = "https://gitcode.com/example/pull/88"


def test_replays_exact_validated_base_then_promotes_and_delivers(tmp_path):
    from scripts.lib.pr_delivery import deliver_validated_candidate

    bundle = _candidate(tmp_path)
    events = []
    workspace = Workspace(tmp_path / "promotion")
    class Client:
        def get_issue(self, **kwargs):
            events.append(("get_issue", kwargs))
            return {"id": 152212, "number": 72}

    client = Client()

    def clone(source, destination, *, branch):
        events.append(("clone", source, destination, branch))
        return workspace

    def promote(**kwargs):
        events.append(("promote", kwargs))
        return Promotion(branch=kwargs["branch"])

    def deliver(**kwargs):
        events.append(("deliver", kwargs))
        return Resource()

    result = deliver_validated_candidate(
        candidate_dir=bundle.root,
        expected_run_id="123456",
        workspace_dir=tmp_path / "promotion",
        target_source="https://gitcode.com/upstream.git",
        config=_config(),
        username="qq_42020325",
        token="secret",
        delivery_run_id="654321",
        delivery_run_attempt="2",
        source_issue_number=72,
        clone=clone,
        promote=promote,
        client_factory=lambda **kwargs: client,
        deliver=deliver,
    )

    assert result == Resource()
    assert [event[0] for event in events] == [
        "clone",
        "promote",
        "get_issue",
        "deliver",
    ]
    assert events[0][3] == "master"
    assert events[1][1]["expected_run_id"] == "123456"
    assert events[1][1]["branch"] == (
        "auto/new-image/kvrocks/2.16.0-oe2403sp4-e2e-654321-a2"
    )
    issue_lookup = events[2][1]
    assert issue_lookup == {
        "target_repo": "openeuler/openeuler-docker-images",
        "number": 72,
    }
    delivery = events[3][1]
    assert delivery["repo"] == workspace.path
    assert delivery["client"] is client
    assert delivery["promotion"].branch == events[1][1]["branch"]
    assert delivery["issue_id"] == "152212"
    assert bundle.manifest.content_sha256 in delivery["body"]
    assert delivery["title"].startswith("[New Image] Add kvrocks")


def test_wrong_validated_run_stops_before_clone_or_delivery(tmp_path):
    from scripts.lib.candidate_bundle import CandidateBundleError
    from scripts.lib.pr_delivery import deliver_validated_candidate

    bundle = _candidate(tmp_path)
    events = []

    with pytest.raises(CandidateBundleError, match="run ID"):
        deliver_validated_candidate(
            candidate_dir=bundle.root,
            expected_run_id="999999",
            workspace_dir=tmp_path / "promotion",
            target_source="https://gitcode.com/upstream.git",
            config=_config(),
            username="qq_42020325",
            token="secret",
            delivery_run_id="654321",
            delivery_run_attempt="1",
            clone=lambda *args, **kwargs: events.append("clone"),
            promote=lambda **kwargs: events.append("promote"),
            client_factory=lambda **kwargs: events.append("client"),
            deliver=lambda **kwargs: events.append("deliver"),
        )

    assert events == []


def test_changed_target_master_does_not_block_promotion_or_delivery(tmp_path):
    from scripts.lib.pr_delivery import deliver_validated_candidate

    bundle = _candidate(tmp_path)
    events = []

    deliver_validated_candidate(
        candidate_dir=bundle.root,
        expected_run_id="123456",
        workspace_dir=tmp_path / "promotion",
        target_source="https://gitcode.com/upstream.git",
        config=_config(),
        username="qq_42020325",
        token="secret",
        delivery_run_id="654321",
        delivery_run_attempt="1",
        clone=lambda *args, **kwargs: Workspace(
            tmp_path / "promotion",
            base_sha="9" * 40,
        ),
        promote=lambda **kwargs: events.append("promote"),
        client_factory=lambda **kwargs: events.append("client"),
        deliver=lambda **kwargs: events.append("deliver"),
    )

    assert events == ["promote", "client", "deliver"]


def test_validate_only_and_missing_token_stop_before_clone(tmp_path):
    from scripts.lib.pr_delivery import (
        ForkPRPipelineError,
        deliver_validated_candidate,
    )

    bundle = _candidate(tmp_path)
    events = []
    common = {
        "candidate_dir": bundle.root,
        "expected_run_id": "123456",
        "workspace_dir": tmp_path / "promotion",
        "target_source": "https://gitcode.com/upstream.git",
        "username": "qq_42020325",
        "delivery_run_id": "654321",
        "delivery_run_attempt": "1",
        "clone": lambda *args, **kwargs: events.append("clone"),
        "promote": lambda **kwargs: events.append("promote"),
        "client_factory": lambda **kwargs: events.append("client"),
        "deliver": lambda **kwargs: events.append("deliver"),
    }

    with pytest.raises(ForkPRPipelineError, match="fork_pr"):
        deliver_validated_candidate(
            **common,
            config=_config("validate_only"),
            token="secret",
        )
    with pytest.raises(ForkPRPipelineError, match="token"):
        deliver_validated_candidate(
            **common,
            config=_config(),
            token="",
        )

    assert events == []


@pytest.mark.parametrize(
    ("run_id", "attempt"),
    (("", "1"), ("0", "1"), ("abc", "1"), ("123", "0"), ("123", "x")),
)
def test_invalid_delivery_identity_stops_before_clone(tmp_path, run_id, attempt):
    from scripts.lib.pr_delivery import (
        ForkPRPipelineError,
        deliver_validated_candidate,
    )

    bundle = _candidate(tmp_path)
    events = []

    with pytest.raises(ForkPRPipelineError, match="delivery run"):
        deliver_validated_candidate(
            candidate_dir=bundle.root,
            expected_run_id="123456",
            workspace_dir=tmp_path / "promotion",
            target_source="https://gitcode.com/upstream.git",
            config=_config(),
            username="qq_42020325",
            token="secret",
            delivery_run_id=run_id,
            delivery_run_attempt=attempt,
            clone=lambda *args, **kwargs: events.append("clone"),
            promote=lambda **kwargs: events.append("promote"),
            client_factory=lambda **kwargs: events.append("client"),
            deliver=lambda **kwargs: events.append("deliver"),
        )

    assert events == []
