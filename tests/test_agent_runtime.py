import json
import re
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
    from scripts.lib.agent_runtime import run_agent

    executable = tmp_path / "opencode"
    workspace = tmp_path / "target"
    workspace.mkdir()
    payload = json.dumps({"success": True, "files_created": ["meta.yml"]})
    event = json.dumps({"type": "text", "part": {"text": payload}})
    step = json.dumps({"type": "step_start", "part": {"id": "step-1"}})
    tool = json.dumps(
        {
            "type": "tool_use",
            "part": {
                "tool": "read",
                "state": {
                    "status": "completed",
                    "input": {
                        "filePath": str(
                            workspace / "Database" / "kvrocks" / "meta.yml"
                        )
                    },
                    "metadata": {
                        "display": {
                            "text": "low-value file contents deepseek-secret"
                        }
                    },
                    "output": "low-value file contents deepseek-secret",
                },
            },
        }
    )
    write_tool = json.dumps(
        {
            "type": "tool_use",
            "part": {
                "tool": "write",
                "state": {
                    "status": "completed",
                    "input": {
                        "filePath": str(
                            workspace / "Database" / "kvrocks" / "README.md"
                        ),
                        "content": "low-value generated contents deepseek-secret",
                    },
                    "metadata": {"exists": False},
                    "output": "Wrote file successfully.",
                },
            },
        }
    )
    executable.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' '{step}'\n"
        f"printf '%s\\n' '{tool}'\n"
        f"printf '%s\\n' '{write_tool}'\n"
        f"printf '%s\\n' '{event}'\n"
    )
    executable.chmod(0o755)

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
    assert (
        "[flow][agent:image_creator] START "
        "model=deepseek/deepseek-v4-flash timeout=1800s"
    ) in output
    assert (
        "[flow][agent:image_creator] ACTION tool=write "
        "path=Database/kvrocks/README.md status=completed"
    ) in output
    assert "ACTION tool=read" not in output
    assert "[flow][agent:image_creator] MESSAGE " in output
    assert (
        "[flow][agent:image_creator] ACTIVITY "
        "messages=1 actions=2 tools=read:1,write:1"
    ) in output
    assert '"files_created": ["meta.yml"]' in output
    assert "low-value file contents" not in output
    assert "metadata" not in output
    assert re.search(
        r"\[flow\]\[agent:image_creator\] PASS elapsed=\d+\.\d+s",
        output,
    )
    assert "EVENT" not in output
    assert "deepseek-secret" not in output


def test_default_agent_runner_logs_heartbeat_before_timeout(
    tmp_path,
    capsys,
    monkeypatch,
):
    from scripts.lib import agent_runtime

    executable = tmp_path / "opencode"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import time\n"
        "time.sleep(1)\n"
    )
    executable.chmod(0o755)
    workspace = tmp_path / "target"
    workspace.mkdir()
    monkeypatch.setattr(
        agent_runtime,
        "_AGENT_HEARTBEAT_SECONDS",
        0.02,
        raising=False,
    )

    with pytest.raises(
        agent_runtime.AgentRuntimeError,
        match="timed out",
    ):
        agent_runtime.run_agent(
            executable=executable,
            role="image_qa",
            prompt="Review without exposing deepseek-secret.",
            workspace=workspace,
            api_key="deepseek-secret",
            required_keys=("status", "issues", "summary"),
            timeout=0.08,
        )

    output = capsys.readouterr().out
    assert re.search(
        r"\[flow\]\[agent:image_qa\] WAIT "
        r"elapsed=\d+\.\d+s silence=\d+\.\d+s "
        r"last_action=none timeout=0.08s",
        output,
    )
    assert re.search(
        r"\[flow\]\[agent:image_qa\] TIMEOUT elapsed=\d+\.\d+s",
        output,
    )
    assert "deepseek-secret" not in output


def test_write_agent_uses_pinned_model_and_scoped_permissions(tmp_path):
    from scripts.lib.agent_runtime import run_agent

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
    assert call["command"][-4:] == [
        "--dir",
        str(workspace),
        "--",
        "Create the task-scoped image files.",
    ]
    assert "--continue" not in call["command"]
    assert "--session" not in call["command"]
    assert call["command"][-1] == "Create the task-scoped image files."
    assert call["cwd"] == workspace
    assert call["env"]["DEEPSEEK_API_KEY"] == "deepseek-secret"
    config = json.loads(call["env"]["OPENCODE_CONFIG_CONTENT"])
    assert config["permission"]["edit"] == "allow"
    assert config["permission"]["bash"] == "allow"
    assert config["permission"]["task"] == "deny"
    assert config["permission"]["external_directory"] == "deny"


def test_qa_agent_is_read_only_and_parses_multiline_json_from_event(tmp_path):
    from scripts.lib.agent_runtime import run_agent

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
    assert config["permission"]["read"] == "deny"
    assert config["permission"]["bash"] == "deny"
    assert config["permission"]["webfetch"] == "deny"
    assert config["permission"]["task"] == "deny"


def test_agent_failure_and_parse_errors_never_expose_api_key(tmp_path):
    from scripts.lib.agent_runtime import AgentRuntimeError, run_agent

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
    from scripts.lib.agent_runtime import AgentRuntimeError, run_agent

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


def test_agent_ignores_contract_shaped_json_from_tool_output(tmp_path):
    from scripts.lib.agent_runtime import AgentRuntimeError, run_agent

    executable = _executable(tmp_path)
    workspace = tmp_path / "target"
    workspace.mkdir()
    tool_event = {
        "type": "tool_use",
        "part": {
            "tool": "read",
            "state": {
                "status": "completed",
                "output": json.dumps(
                    {"status": "approved", "issues": [], "summary": "not final"}
                ),
            },
        },
    }
    text_event = {
        "type": "text",
        "part": {"text": "I inspected the files but have no final report."},
    }
    runner = RecordingRunner(
        _completed(
            stdout=json.dumps(tool_event)
            + "\n"
            + json.dumps(text_event)
            + "\n"
        )
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


def test_agent_rejects_invalid_contract_value_types(tmp_path):
    from scripts.lib.agent_runtime import AgentRuntimeError, run_agent

    executable = _executable(tmp_path)
    workspace = tmp_path / "target"
    workspace.mkdir()
    event = {
        "type": "text",
        "part": {
            "text": json.dumps(
                {
                    "success": "yes",
                    "files_created": ["Database/kvrocks/meta.yml"],
                }
            )
        },
    }
    runner = RecordingRunner(_completed(stdout=json.dumps(event) + "\n"))

    with pytest.raises(AgentRuntimeError, match="success.*boolean"):
        run_agent(
            executable=executable,
            role="image_creator",
            prompt="Create.",
            workspace=workspace,
            api_key="deepseek-secret",
            required_keys=("success", "files_created"),
            runner=runner,
        )


def test_fixer_contract_allows_role_specific_status_value(tmp_path):
    from scripts.lib.agent_runtime import run_agent

    executable = _executable(tmp_path)
    workspace = tmp_path / "target"
    workspace.mkdir()
    payload = {
        "success": False,
        "status": "insufficient_evidence",
        "changes": [],
        "summary": "The failure excerpt has no root cause.",
    }
    event = {"type": "text", "part": {"text": json.dumps(payload)}}
    runner = RecordingRunner(_completed(stdout=json.dumps(event) + "\n"))

    result = run_agent(
        executable=executable,
        role="fixer",
        prompt="Diagnose the native failure.",
        workspace=workspace,
        api_key="deepseek-secret",
        required_keys=("success", "changes"),
        runner=runner,
    )

    assert result.payload == payload


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


def _git_workspace(tmp_path):
    workspace = tmp_path / "target"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    return workspace


def test_agent_gets_a_scratch_dir_the_candidate_gate_cannot_see(tmp_path):
    """The target repo used to be the Agent's only writable place.

    Run 30567356119 unpacked an upstream tarball there and the gate reported
    496 out-of-scope files, so give research output somewhere legitimate.
    """
    from scripts.lib.agent_runtime import run_agent

    executable = _executable(tmp_path)
    workspace = _git_workspace(tmp_path)
    payload = json.dumps({"success": True, "files_created": []})
    runner = RecordingRunner(
        _completed(
            stdout=json.dumps({"type": "text", "part": {"text": payload}})
        )
    )

    run_agent(
        executable=executable,
        role="image_creator",
        prompt="Create the image.",
        workspace=workspace,
        api_key="deepseek-secret",
        required_keys=("success", "files_created"),
        runner=runner,
    )

    scratch = runner.calls[0]["env"]["OE_AGENT_SCRATCH"]
    assert scratch.startswith(str(workspace))
    (workspace / scratch[len(str(workspace)) + 1 :] / "upstream.tar").write_text(
        "junk"
    )
    status = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == ""


def test_scratch_dir_survives_a_workspace_without_git(tmp_path):
    """Smoke and demo paths hand over plain directories."""
    from scripts.lib.agent_runtime import run_agent

    executable = _executable(tmp_path)
    workspace = tmp_path / "plain"
    workspace.mkdir()
    payload = json.dumps({"success": True, "files_created": []})
    runner = RecordingRunner(
        _completed(
            stdout=json.dumps({"type": "text", "part": {"text": payload}})
        )
    )

    run_agent(
        executable=executable,
        role="image_creator",
        prompt="Create the image.",
        workspace=workspace,
        api_key="deepseek-secret",
        required_keys=("success", "files_created"),
        runner=runner,
    )

    from pathlib import Path

    assert Path(runner.calls[0]["env"]["OE_AGENT_SCRATCH"]).is_dir()


class SequenceRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, command, cwd, env, timeout):
        self.calls.append({"command": list(command), "timeout": timeout})
        return self.results.pop(0)


def test_a_silent_timeout_is_retried_instead_of_failing_the_round(tmp_path):
    """Run 30597380057: image QA produced messages=0 actions=0 for 180s.

    Nothing was attempted, so this is the provider hanging rather than the
    Agent overrunning its boundary, and failing the job made the run repay a
    436s image_creator that had already succeeded.
    """
    from scripts.lib.agent_runtime import run_agent

    executable = _executable(tmp_path)
    workspace = tmp_path / "target"
    workspace.mkdir()
    payload = {"status": "approved", "issues": [], "summary": "fine"}
    runner = SequenceRunner(
        [
            _completed(returncode=124, stdout=""),
            _completed(
                stdout=json.dumps(
                    {"type": "text", "part": {"text": json.dumps(payload)}}
                )
            ),
        ]
    )

    result = run_agent(
        executable=executable,
        role="image_qa",
        prompt="Review the image.",
        workspace=workspace,
        api_key="deepseek-secret",
        required_keys=("status", "issues", "summary"),
        runner=runner,
        timeout=180,
    )

    assert result.payload == payload
    assert len(runner.calls) == 2


def test_a_timeout_after_real_activity_is_not_retried(tmp_path):
    """A stalled Agent that did work is a boundary problem, not a hang.

    Retrying it would pay the same cost again for the same reason.
    """
    from scripts.lib.agent_runtime import AgentRuntimeError, run_agent

    executable = _executable(tmp_path)
    workspace = tmp_path / "target"
    workspace.mkdir()
    active = json.dumps(
        {
            "type": "tool_use",
            "part": {"tool": "bash", "state": {"status": "completed"}},
        }
    )
    runner = SequenceRunner([_completed(returncode=124, stdout=active)])

    with pytest.raises(AgentRuntimeError, match="timed out"):
        run_agent(
            executable=executable,
            role="image_qa",
            prompt="Review the image.",
            workspace=workspace,
            api_key="deepseek-secret",
            required_keys=("status", "issues", "summary"),
            runner=runner,
            timeout=180,
        )

    assert len(runner.calls) == 1
