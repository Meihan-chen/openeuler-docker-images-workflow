import json
import subprocess

import pytest


class RecordingRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, command, cwd, env, timeout):
        self.calls.append(
            {
                "command": list(command),
                "cwd": cwd,
                "env": dict(env),
                "timeout": timeout,
            }
        )
        return self.result


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _executable(tmp_path):
    path = tmp_path / "opencode"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_default_agent_runner_streams_safe_progress(tmp_path, capsys):
    from scripts.harness.run import run_agent

    executable = tmp_path / "opencode"
    payload = json.dumps({"success": True, "files_created": ["meta.yml"]})
    event = json.dumps({"type": "text", "part": {"text": payload}})
    executable.write_text(f"#!/bin/sh\nprintf '%s\\n' '{event}'\n")
    executable.chmod(0o755)
    workspace = tmp_path / "target"
    workspace.mkdir()

    result = run_agent(
        executable=executable,
        role="image_creator",
        prompt="secret-safe prompt",
        workspace=workspace,
        api_key="deepseek-secret",
        required_keys=("success", "files_created"),
    )

    output = capsys.readouterr().out
    assert result.payload["success"] is True
    assert "[flow][agent:image_creator] START" in output
    assert "[flow][agent:image_creator] EVENT text" in output
    assert "[flow][agent:image_creator] PASS" in output
    assert "deepseek-secret" not in output
    assert payload not in output


def test_write_agent_uses_pinned_model_and_scoped_permissions(tmp_path):
    from scripts.harness.run import run_agent

    executable = _executable(tmp_path)
    workspace = tmp_path / "target"
    workspace.mkdir()
    event = {
        "type": "text",
        "part": {
            "text": json.dumps(
                {
                    "success": True,
                    "files_created": ["Database/kvrocks/meta.yml"],
                }
            )
        },
    }
    runner = RecordingRunner(_completed(stdout=json.dumps(event) + "\n"))

    result = run_agent(
        executable=executable,
        role="image_creator",
        prompt="Create the task-scoped image files.",
        workspace=workspace,
        api_key="deepseek-secret",
        required_keys=("success", "files_created"),
        runner=runner,
    )

    assert result.payload["success"] is True
    call = runner.calls[0]
    assert call["command"][:7] == [
        str(executable),
        "run",
        "--model",
        "deepseek/deepseek-v4-flash",
        "--format",
        "json",
        "--auto",
    ]
    assert call["command"][-1] == "Create the task-scoped image files."
    assert call["cwd"] == workspace
    assert call["env"]["DEEPSEEK_API_KEY"] == "deepseek-secret"
    config = json.loads(call["env"]["OPENCODE_CONFIG_CONTENT"])
    assert config["permission"]["edit"] == "allow"
    assert config["permission"]["bash"] == "allow"
    assert config["permission"]["external_directory"] == "deny"


def test_qa_agent_is_read_only_and_parses_multiline_json_from_event(tmp_path):
    from scripts.harness.run import run_agent

    executable = _executable(tmp_path)
    workspace = tmp_path / "target"
    workspace.mkdir()
    review = {
        "status": "approved",
        "issues": [],
        "coverage_score": 0.95,
        "summary": "Both architecture-sensitive paths are covered.",
    }
    event = {
        "type": "text",
        "part": {
            "text": "Review complete:\n```json\n"
            + json.dumps(review, indent=2)
            + "\n```",
        },
    }
    runner = RecordingRunner(_completed(stdout=json.dumps(event) + "\n"))

    result = run_agent(
        executable=executable,
        role="testcase_qa",
        prompt="Review only.",
        workspace=workspace,
        api_key="deepseek-secret",
        required_keys=("status", "issues", "coverage_score", "summary"),
        runner=runner,
    )

    assert result.payload == review
    config = json.loads(
        runner.calls[0]["env"]["OPENCODE_CONFIG_CONTENT"]
    )
    assert config["permission"]["edit"] == "deny"
    assert config["permission"]["bash"] == "deny"
    assert config["permission"]["webfetch"] == "deny"


def test_agent_failure_and_parse_errors_never_expose_api_key(tmp_path):
    from scripts.harness.run import AgentRuntimeError, run_agent

    executable = _executable(tmp_path)
    workspace = tmp_path / "target"
    workspace.mkdir()
    key = "deepseek-key-must-not-leak"
    runner = RecordingRunner(
        _completed(returncode=1, stderr=f"provider rejected {key}")
    )

    with pytest.raises(AgentRuntimeError) as error:
        run_agent(
            executable=executable,
            role="image_creator",
            prompt="Create.",
            workspace=workspace,
            api_key=key,
            required_keys=("success",),
            runner=runner,
        )

    assert key not in str(error.value)
    assert "REDACTED" in str(error.value)


def test_agent_rejects_successful_process_without_required_json_contract(tmp_path):
    from scripts.harness.run import AgentRuntimeError, run_agent

    executable = _executable(tmp_path)
    workspace = tmp_path / "target"
    workspace.mkdir()
    runner = RecordingRunner(
        _completed(stdout='{"type":"text","part":{"text":"looks good"}}\n')
    )

    with pytest.raises(AgentRuntimeError, match="JSON contract"):
        run_agent(
            executable=executable,
            role="image_qa",
            prompt="Review.",
            workspace=workspace,
            api_key="deepseek-secret",
            required_keys=("status", "issues", "summary"),
            runner=runner,
        )


def test_shared_legacy_adversarial_entrypoint_records_disagreement_and_continues(
    tmp_path, monkeypatch
):
    from scripts.harness import run

    responses = [
        '{"success": true}',
        '{"status": "needs_fix", "issues": ["broken"]}',
        '{"success": true}',
        '{"status": "needs_fix", "issues": ["still broken"]}',
    ]
    monkeypatch.setattr(run, "_target_dir", lambda: tmp_path)
    monkeypatch.setattr(
        run,
        "_run_opencode",
        lambda *args, **kwargs: responses.pop(0),
    )
    monkeypatch.setattr(run, "_write_qa_record", lambda *args, **kwargs: None)

    run._run_adversarial_pair("image")

    assert responses == []
