import json

import pytest


def test_legacy_qa_prompt_receives_creator_payload_for_every_scenario(
    tmp_path,
    monkeypatch,
):
    from scripts.harness import run

    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(run, "_target_dir", lambda: target)
    monkeypatch.setenv("APP", "example-app")
    monkeypatch.setenv("PACKAGE", "example-app")
    monkeypatch.setenv("DOMAIN", "Cloud")
    payload = {
        "success": True,
        "files_created": ["Cloud/example-app/meta.yml"],
        "identity_decision": {
            "mode": "dynamic",
            "user": "example-app",
            "group": "example-app",
            "uid": None,
            "gid": None,
            "requirement_evidence_ids": [],
        },
        "evidence_requests": [],
    }
    resolved = {
        "status": "resolved",
        "scenario": "version-update",
        "entries": [],
    }

    for scenario in ("new-image", "version-update", "oe-upgrade"):
        monkeypatch.setenv("SCENARIO", scenario)
        prompt = run._build_qa_prompt(
            "image",
            creator_payload=payload,
            resolved_evidence=resolved,
        )

        assert scenario in prompt
        assert "Creator structured result" in prompt
        assert json.dumps(payload, ensure_ascii=False, indent=2) in prompt
        assert "Harness-resolved evidence bundle" in prompt
        assert json.dumps(resolved, ensure_ascii=False, indent=2) in prompt


def test_legacy_pair_passes_latest_creator_payload_to_each_qa_round(
    tmp_path,
    monkeypatch,
):
    from scripts.harness import run

    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(run, "_target_dir", lambda: target)
    monkeypatch.setenv("APP", "example-app")
    monkeypatch.setenv("PACKAGE", "example-app")
    monkeypatch.setenv("DOMAIN", "Cloud")
    initial = {
        "success": True,
        "files_created": [],
        "identity_decision": {
            "mode": "dynamic",
            "user": "example-app",
            "group": "example-app",
            "uid": None,
            "gid": None,
            "requirement_evidence_ids": [],
        },
        "evidence_requests": [
            {
                "id": "identity-initial",
                "claim": "initial identity claim",
                "source": (
                    "https://github.com/acme/example-app/blob/v1.2.3/Dockerfile"
                ),
                "locator": "USER example-app",
            }
        ],
    }
    repaired = {
        **initial,
        "identity_decision": {
            **initial["identity_decision"],
            "mode": "reuse_existing",
        },
        "evidence_requests": [
            {
                "id": "identity-repaired",
                "claim": "repaired identity claim",
                "source": (
                    "https://github.com/acme/example-app/blob/v1.2.3/Dockerfile"
                ),
                "locator": "USER packaged-app",
            }
        ],
    }
    responses = iter(
        (
            json.dumps(initial),
            json.dumps(
                {
                    "status": "needs_fix",
                    "issues": [{"description": "reuse packaged account"}],
                    "summary": "repair identity",
                }
            ),
            json.dumps(repaired),
            json.dumps(
                {
                    "status": "needs_fix",
                    "issues": [{"description": "still disputed"}],
                    "summary": "local validation decides",
                }
            ),
        )
    )
    qa_payloads = []
    original_qa_prompt = run._build_qa_prompt

    def record_qa_prompt(role, *, creator_payload, resolved_evidence=None):
        qa_payloads.append(creator_payload)
        return original_qa_prompt(
            role,
            creator_payload=creator_payload,
            resolved_evidence=resolved_evidence,
        )

    monkeypatch.setattr(run, "_build_qa_prompt", record_qa_prompt)
    monkeypatch.setattr(run, "_run_opencode", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setenv("APP_VERSION", "1.2.3")
    monkeypatch.setenv("OS_VERSION", "24.03-lts-sp4")
    monkeypatch.setenv("SOURCE", "https://github.com/acme/example-app/tree/v1.2.3")
    monkeypatch.setenv("SCENARIO", "version-update")
    resolved_request_ids = []

    def resolver(*, task, requests):
        resolved_request_ids.append(
            (task.scenario, [request["id"] for request in requests])
        )
        return {
            "status": "resolved",
            "scenario": task.scenario,
            "entries": requests,
        }

    run._run_adversarial_pair("image", evidence_resolver=resolver)

    assert qa_payloads == [initial, repaired]
    assert resolved_request_ids == [
        ("version-update", ["identity-initial"]),
        ("version-update", ["identity-repaired"]),
    ]
    report_dir = tmp_path / "agent-reports" / "Cloud" / "example-app"
    disagreement = json.loads((report_dir / "qa-disagreements.json").read_text())
    assert disagreement["status"] == "pending_local_validation"
    assert disagreement["disagreements"][0]["summary"] == "local validation decides"
    assert (
        report_dir / "image-round1-resolved-evidence.json"
    ).is_file()
    assert (
        report_dir / "image-round2-resolved-evidence.json"
    ).is_file()
    assert not list(target.rglob("*-resolved-evidence.json"))


def test_legacy_unresolved_evidence_stops_before_qa(tmp_path, monkeypatch):
    from scripts.harness import run

    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(run, "_target_dir", lambda: target)
    monkeypatch.setenv("APP", "example-app")
    monkeypatch.setenv("PACKAGE", "example-app")
    monkeypatch.setenv("DOMAIN", "Cloud")
    monkeypatch.setenv("APP_VERSION", "1.2.3")
    monkeypatch.setenv("OS_VERSION", "24.03-lts-sp4")
    monkeypatch.setenv(
        "SOURCE",
        "https://github.com/acme/example-app/tree/v1.2.3",
    )
    creator = {
        "success": True,
        "files_created": [],
        "identity_decision": {
            "mode": "fixed",
            "user": "example-app",
            "group": "example-app",
            "uid": 10001,
            "gid": 10001,
            "requirement_evidence_ids": ["identity-001"],
        },
        "evidence_requests": [
            {
                "id": "identity-001",
                "claim": "upstream requires uid 10001",
                "source": (
                    "https://github.com/acme/example-app/blob/"
                    "v1.2.3/Dockerfile"
                ),
                "locator": "USER 10001",
            }
        ],
    }
    calls = []
    monkeypatch.setattr(
        run,
        "_run_opencode",
        lambda *_args, **_kwargs: calls.append("agent") or json.dumps(creator),
    )

    with pytest.raises(RuntimeError, match="evidence.*unresolved"):
        run._run_adversarial_pair(
            "image",
            evidence_resolver=lambda **_: {
                "status": "unresolved",
                "entries": [{"id": "identity-001", "resolved": False}],
            },
        )

    assert calls == ["agent"]


def test_legacy_creator_uses_the_shared_structured_contract(
    tmp_path,
    monkeypatch,
):
    from scripts.harness import run
    from scripts.lib.agent_runtime import AgentRuntimeError

    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(run, "_target_dir", lambda: target)
    monkeypatch.setenv("APP", "example-app")
    monkeypatch.setenv("DOMAIN", "Cloud")
    monkeypatch.setattr(
        run,
        "_run_opencode",
        lambda *_args, **_kwargs: json.dumps(
            {
                "success": True,
                "files_created": ["Cloud/example-app/tests/test.sh"],
                "command_evidence": [],
                "evidence_requests": [],
            }
        ),
    )

    with pytest.raises(AgentRuntimeError, match="command_evidence.*non-empty"):
        run._run_adversarial_pair("testcase")


def test_legacy_fixer_prompt_inlines_only_verified_matching_knowledge(tmp_path):
    from scripts.harness import run

    prompt = run._build_fixer_prompt(
        target=tmp_path / "target",
        logs={
            "build-amd64.log": (
                "CMake Error: cannot find static library; disable this feature"
            )
        },
        whitelist=["Cloud/example/1.2.3/24.03-lts-sp4/Dockerfile"],
    )

    assert "## Verified failure knowledge" in prompt
    assert "build_feature_dependency_mismatch" in prompt
    assert "runtime_identity_collision" in prompt
    assert "A requested numeric user" not in prompt
    assert "failure-patterns.md" not in prompt
