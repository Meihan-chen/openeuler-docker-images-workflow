import json

import pytest


def test_legacy_image_command_runs_one_creator_pass_without_qa(
    tmp_path,
    monkeypatch,
):
    from scripts.harness import run

    target = tmp_path / "target"
    target.mkdir()
    calls = []
    monkeypatch.setattr(run, "_target_dir", lambda: target)
    monkeypatch.setenv("APP", "example-app")
    monkeypatch.setenv("DOMAIN", "Cloud")
    monkeypatch.setattr(
        run,
        "_run_opencode",
        lambda prompt, **_kwargs: (
            calls.append(prompt)
            or json.dumps(
                {
                    "success": True,
                    "files_created": ["Cloud/example-app/meta.yml"],
                    "identity_decision": {
                        "mode": "reuse_existing",
                        "user": "root",
                        "group": "root",
                        "uid": None,
                        "gid": None,
                    },
                }
            )
        ),
    )

    run._run_adversarial_pair("image")

    assert len(calls) == 1
    assert "Image QA" not in calls[0]


@pytest.mark.parametrize("scenario", ("version-update", "oe-upgrade"))
def test_legacy_image_command_fails_closed_for_unmigrated_scenarios(
    tmp_path,
    monkeypatch,
    scenario,
):
    from scripts.harness import run

    monkeypatch.setattr(run, "_target_dir", lambda: tmp_path)
    monkeypatch.setenv("SCENARIO", scenario)
    monkeypatch.setattr(
        run,
        "_run_opencode",
        lambda *_args, **_kwargs: pytest.fail("Creator must not run"),
    )

    with pytest.raises(RuntimeError, match="has not migrated"):
        run._run_adversarial_pair("image")


def test_legacy_pr_composer_reads_target_testcase_qa_records(
    tmp_path,
    monkeypatch,
):
    from scripts.harness import compose_pr

    target = tmp_path / "target"
    target.mkdir()
    reports = target / "Cloud" / "example-app"
    reports.mkdir(parents=True)
    (reports / "qa-review-testcase-r1.json").write_text(
        json.dumps(
            {
                "round": 1,
                "approved": True,
                "summary": "tests approved",
                "issues": [],
            }
        )
    )
    monkeypatch.setenv("TARGET_REPO_DIR", str(target))
    monkeypatch.setenv("DOMAIN", "Cloud")
    monkeypatch.setenv("APP", "example-app")
    monkeypatch.setenv("PACKAGE", "example-app")

    records = compose_pr._collect_qa_records()

    assert "Testcase Creator ↔ Testcase QA" in records
    assert "tests approved" in records


def test_legacy_pr_body_describes_the_remaining_agent_roles(
    tmp_path,
    monkeypatch,
):
    from scripts.harness import compose_pr

    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setenv("TARGET_REPO_DIR", str(target))
    monkeypatch.setenv("APP", "example-app")
    monkeypatch.setenv("DOMAIN", "Cloud")

    body = compose_pr.compose_pr_body("version-update")

    assert "Image Creator (deterministic gates; no semantic review)" in body
    assert "Testcase Creator + Testcase QA" in body


def test_legacy_qa_prompt_rejects_image_role(tmp_path, monkeypatch):
    from scripts.harness import run

    monkeypatch.setattr(run, "_target_dir", lambda: tmp_path)
    with pytest.raises(ValueError, match="only testcase has a QA prompt"):
        run._build_qa_prompt("image", creator_payload={})


def test_legacy_testcase_qa_prompt_reviews_only_test_files(
    tmp_path,
    monkeypatch,
):
    from scripts.harness import run

    monkeypatch.setattr(run, "_target_dir", lambda: tmp_path)
    monkeypatch.setenv("APP", "example-app")
    monkeypatch.setenv("PACKAGE", "example-app")
    monkeypatch.setenv("DOMAIN", "Cloud")

    prompt = run._build_qa_prompt(
        "testcase",
        creator_payload={"success": True, "files_created": []},
    )

    assert "Cloud/example-app/tests/" in prompt
    assert "Dockerfile (read-only context)" in prompt
    assert "meta.yml" not in prompt
    assert "README.md" not in prompt
    assert "image-list.yml" not in prompt


def test_legacy_testcase_qa_image_issue_does_not_trigger_repair(
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
    creator = {
        "success": True,
        "files_created": ["Cloud/example-app/tests/test.sh"],
        "command_evidence": [
            {
                "command": "example --version",
                "semantics": "prints the version",
            }
        ],
    }
    image_issue = {
        "status": "needs_fix",
        "issues": [
            {
                "file": "Cloud/example-app/1.0/24.03-lts-sp4/Dockerfile",
                "description": "image-only issue",
                "evidence": "Dockerfile content",
            }
        ],
        "summary": "image issue",
    }
    responses = iter((creator, image_issue))
    calls = []
    monkeypatch.setattr(
        run,
        "_run_opencode",
        lambda *_args, **_kwargs: calls.append(1) or json.dumps(next(responses)),
    )
    monkeypatch.setattr(run, "_resolve_creator_evidence", lambda *_args, **_kwargs: {})

    run._run_adversarial_pair("testcase")

    assert len(calls) == 2


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
                "evidence": [],
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
