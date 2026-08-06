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


def test_default_scratch_limit_is_six_gibibytes():
    from scripts.lib import agent_runtime

    assert agent_runtime._SCRATCH_LIMIT_MB == 6000


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
            role="testcase_qa",
            prompt="Review without exposing deepseek-secret.",
            workspace=workspace,
            api_key="deepseek-secret",
            required_keys=("status", "issues", "summary"),
            timeout=0.08,
        )

    output = capsys.readouterr().out
    assert re.search(
        r"\[flow\]\[agent:testcase_qa\] WAIT "
        r"elapsed=\d+\.\d+s silence=\d+\.\d+s "
        r"last_action=none timeout=0.08s",
        output,
    )
    assert re.search(
        r"\[flow\]\[agent:testcase_qa\] TIMEOUT elapsed=\d+\.\d+s",
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


def test_fixer_can_read_only_declared_external_evidence_directories(tmp_path):
    from scripts.lib.agent_runtime import run_agent

    executable = _executable(tmp_path)
    workspace = tmp_path / "target"
    workspace.mkdir()
    evidence_dirs = (
        tmp_path / "phase1-x86" / "diagnostics",
        tmp_path / "phase1-arm" / "diagnostics",
    )
    for directory in evidence_dirs:
        directory.mkdir(parents=True)
    event = {
        "type": "text",
        "part": {
            "text": json.dumps(
                {
                    "success": True,
                    "changes": [],
                }
            )
        },
    }
    runner = RecordingRunner(_completed(stdout=json.dumps(event) + "\n"))

    run_agent(
        executable=executable,
        role="fixer",
        prompt="Inspect the declared native evidence when useful.",
        workspace=workspace,
        api_key="deepseek-secret",
        required_keys=("success", "changes"),
        runner=runner,
        external_read_dirs=evidence_dirs,
    )

    config = json.loads(runner.calls[0]["env"]["OPENCODE_CONFIG_CONTENT"])
    patterns = tuple(f"{directory.resolve()}/**" for directory in evidence_dirs)
    assert list(config["permission"]["external_directory"].items()) == [
        ("*", "deny"),
        (patterns[0], "allow"),
        (patterns[1], "allow"),
    ]
    # OpenCode evaluates edit/write paths relative to the Agent worktree, so
    # absolute evidence paths cannot enforce this boundary.  Reject every
    # parent-relative target after the workspace-wide allow rule instead.
    assert list(config["permission"]["edit"].items()) == [
        ("*", "allow"),
        ("../**", "deny"),
    ]


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


def test_direct_qa_contract_validation_remains_strict():
    from scripts.lib.agent_runtime import AgentRuntimeError, validate_agent_payload

    with pytest.raises(AgentRuntimeError, match="status.*approved or needs_fix"):
        validate_agent_payload(
            {
                "status": "looks_good",
                "issues": [],
                "coverage_score": 0.9,
                "summary": "The candidate looks correct.",
            },
            required_keys=("status", "issues", "coverage_score", "summary"),
        )

    with pytest.raises(AgentRuntimeError, match="coverage_score.*between 0 and 1"):
        validate_agent_payload(
            {
                "status": "approved",
                "issues": [],
                "coverage_score": "high",
                "summary": "The candidate looks correct.",
            },
            required_keys=("status", "issues", "coverage_score", "summary"),
        )


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
            role="testcase_qa",
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
            role="testcase_qa",
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


def test_shared_legacy_testcase_pair_records_disagreement_and_continues(
    tmp_path, monkeypatch
):
    from scripts.harness import run

    creator = json.dumps(
        {
            "success": True,
            "files_created": [],
            "command_evidence": [
                {
                    "command": "example --version",
                    "semantics": "prints the installed version",
                    "evidence_id": "command-version",
                }
            ],
            "evidence": [],
        }
    )
    responses = [
        creator,
        json.dumps(
            {
                "status": "needs_fix",
                "issues": [
                    {
                        "file": "Cloud/example/tests/test.sh",
                        "description": "broken",
                        "evidence": "test.sh",
                    }
                ],
            }
        ),
        creator,
        json.dumps(
            {
                "status": "needs_fix",
                "issues": [
                    {
                        "file": "Cloud/example/tests/test.sh",
                        "description": "still broken",
                        "evidence": "test.sh",
                    }
                ],
            }
        ),
    ]
    monkeypatch.setattr(run, "_target_dir", lambda: tmp_path)
    monkeypatch.setenv("DOMAIN", "Cloud")
    monkeypatch.setenv("APP", "example")
    monkeypatch.setattr(
        run,
        "_run_opencode",
        lambda *args, **kwargs: responses.pop(0),
    )
    monkeypatch.setattr(run, "_write_qa_record", lambda *args, **kwargs: None)

    run._run_adversarial_pair("testcase")

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
    """Run 30597380057: a QA produced messages=0 actions=0 for 180s.

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
        role="testcase_qa",
        prompt="Review the testcases.",
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
            role="testcase_qa",
            prompt="Review the testcases.",
            workspace=workspace,
            api_key="deepseek-secret",
            required_keys=("status", "issues", "summary"),
            runner=runner,
            timeout=180,
        )

    assert len(runner.calls) == 1


def test_lifecycle_events_alone_do_not_count_as_agent_activity(tmp_path):
    """Run 30597380057 reported messages=0 actions=0 tools=none.

    Only text and tool_use feed that counter, so treating any event with a type
    as activity would let a provider that hung right after step_start suppress
    the retry this exists for. It also keeps the retry safe for write roles:
    with no tool call there is no half-finished edit to repeat.
    """
    from scripts.lib.agent_runtime import run_agent

    executable = _executable(tmp_path)
    workspace = tmp_path / "target"
    workspace.mkdir()
    payload = {"success": True, "changes": []}
    runner = SequenceRunner(
        [
            _completed(
                returncode=124,
                stdout=json.dumps(
                    {"type": "step_start", "part": {"id": "step-1"}}
                ),
            ),
            _completed(
                stdout=json.dumps(
                    {"type": "text", "part": {"text": json.dumps(payload)}}
                )
            ),
        ]
    )

    result = run_agent(
        executable=executable,
        role="fixer",
        prompt="Repair the candidate.",
        workspace=workspace,
        api_key="deepseek-secret",
        required_keys=("success", "changes"),
        runner=runner,
        timeout=180,
    )

    assert result.payload == payload
    assert len(runner.calls) == 2


def _testcase_creator_payload(**overrides):
    payload = {
        "success": True,
        "files_created": ["Database/kvrocks/tests/test.sh"],
        "command_evidence": [
            {
                "command": "redis-cli GET",
                "semantics": "returns the value stored for the key",
                "evidence_id": "command-get-001",
            }
        ],
        "evidence": [
            {
                "id": "command-get-001",
                "claim": "GET returns the value stored for the key",
                "source": (
                    "https://github.com/apache/kvrocks/blob/"
                    "v2.16.0/src/commands/cmd_string.cc"
                ),
                "excerpts": ["Get::Execute"],
            }
        ],
    }
    payload.update(overrides)
    return payload


def _run_testcase_creator(tmp_path, payload):
    from scripts.lib.agent_runtime import run_agent

    executable = _executable(tmp_path)
    workspace = tmp_path / "target"
    workspace.mkdir()
    event = {"type": "text", "part": {"text": json.dumps(payload)}}
    runner = RecordingRunner(_completed(stdout=json.dumps(event) + "\n"))

    return run_agent(
        executable=executable,
        role="testcase_creator",
        prompt="Write the tests.",
        workspace=workspace,
        api_key="deepseek-secret",
        required_keys=(
            "success",
            "files_created",
            "command_evidence",
        ),
        runner=runner,
    )


def test_testcase_contract_accepts_cited_command_evidence(tmp_path):
    payload = _testcase_creator_payload()

    result = _run_testcase_creator(tmp_path, payload)

    assert result.payload == payload


def test_testcase_contract_rejects_empty_command_evidence(tmp_path):
    """Every application command under test needs a semantics citation.

    Run 30781977554 asserted Redis semantics for the Kvrocks `DBSIZE` command
    and spent the last repair round on it; nothing had asked where the
    expectation came from.
    """
    from scripts.lib.agent_runtime import AgentRuntimeError

    with pytest.raises(AgentRuntimeError, match="command_evidence.*non-empty"):
        _run_testcase_creator(
            tmp_path,
            _testcase_creator_payload(command_evidence=[]),
        )


@pytest.mark.parametrize("field", ["command", "semantics"])
def test_testcase_contract_rejects_command_evidence_missing_a_field(
    tmp_path,
    field,
):
    from scripts.lib.agent_runtime import AgentRuntimeError

    entry = {
        "command": "redis-cli DBSIZE",
        "semantics": "returns a cached count refreshed by DBSIZE SCAN",
        "evidence_id": "command-get-001",
    }
    entry[field] = "  "

    with pytest.raises(AgentRuntimeError, match=f"non-empty {field}"):
        _run_testcase_creator(
            tmp_path,
            _testcase_creator_payload(command_evidence=[entry]),
        )


def _run_image_creator(tmp_path, identity_decision, evidence=None):
    from scripts.lib.agent_runtime import run_agent

    executable = _executable(tmp_path)
    workspace = tmp_path / "target"
    workspace.mkdir()
    payload = {
        "success": True,
        "files_created": ["Database/kvrocks/meta.yml"],
        "identity_decision": identity_decision,
        "evidence": evidence or [],
    }
    event = {"type": "text", "part": {"text": json.dumps(payload)}}
    runner = RecordingRunner(_completed(stdout=json.dumps(event) + "\n"))
    return run_agent(
        executable=executable,
        role="image_creator",
        prompt="Write the image.",
        workspace=workspace,
        api_key="deepseek-secret",
        required_keys=(
            "success",
            "files_created",
            "identity_decision",
        ),
        runner=runner,
    )


def test_image_contract_accepts_dynamic_identity_decision(tmp_path):
    decision = {
        "mode": "dynamic",
        "user": "kvrocks",
        "group": "kvrocks",
        "uid": None,
        "gid": None,
        "requirement_evidence_ids": [],
    }

    result = _run_image_creator(tmp_path, decision)

    assert result.payload["identity_decision"] == decision


def test_generation_runtime_keys_leave_creator_contract_for_the_gate(
    tmp_path,
):
    from scripts.lib.agent_runtime import run_agent

    executable = _executable(tmp_path)
    workspace = tmp_path / "target"
    workspace.mkdir()
    payload = {
        "success": True,
        "files_created": ["Bigdata/kylin-e2e-test/meta.yml"],
        "identity_decision": {
            "mode": "dynamic",
            "user": None,
            "group": None,
            "uid": None,
            "gid": None,
            "requirement_evidence_ids": [],
        },
    }
    event = {"type": "text", "part": {"text": json.dumps(payload)}}
    runner = RecordingRunner(_completed(stdout=json.dumps(event) + "\n"))

    result = run_agent(
        executable=executable,
        role="image_creator",
        prompt="Write the image.",
        workspace=workspace,
        api_key="deepseek-secret",
        required_keys=("success", "files_created"),
        runner=runner,
    )

    assert result.payload == payload


def test_image_qa_role_is_unsupported(tmp_path):
    from scripts.lib.agent_runtime import AgentRuntimeError, run_agent

    executable = _executable(tmp_path)
    workspace = tmp_path / "target"
    workspace.mkdir()
    payload = {
        "issues": [],
        "summary": "No actionable candidate issue was described.",
    }
    event = {"type": "text", "part": {"text": json.dumps(payload)}}
    runner = RecordingRunner(_completed(stdout=json.dumps(event) + "\n"))

    with pytest.raises(AgentRuntimeError, match="unsupported Agent role: image_qa"):
        run_agent(
            executable=executable,
            role="image_qa",
            prompt="Review the image.",
            workspace=workspace,
            api_key="deepseek-secret",
            required_keys=("issues", "summary"),
            runner=runner,
        )


def test_image_contract_rejects_fixed_identity_without_semantic_review(
    tmp_path,
):
    from scripts.lib.agent_runtime import AgentRuntimeError

    with pytest.raises(
        AgentRuntimeError,
        match="identity_decision mode must be dynamic or reuse_existing",
    ):
        _run_image_creator(
            tmp_path,
            {
                "mode": "fixed",
                "user": "kvrocks",
                "group": "kvrocks",
                "uid": 991,
                "gid": 991,
            },
        )


def test_image_contract_rejects_dynamic_identity_with_numeric_ids(tmp_path):
    from scripts.lib.agent_runtime import AgentRuntimeError

    with pytest.raises(AgentRuntimeError, match="dynamic identity.*null"):
        _run_image_creator(
            tmp_path,
            {
                "mode": "dynamic",
                "user": "kvrocks",
                "group": "kvrocks",
                "uid": 991,
                "gid": 991,
                "requirement_evidence_ids": [],
            },
        )


def test_image_contract_rejects_fixed_identity_even_with_legacy_evidence(
    tmp_path,
):
    from scripts.lib.agent_runtime import AgentRuntimeError

    decision = {
        "mode": "fixed",
        "user": "example",
        "group": "example",
        "uid": 10001,
        "gid": 10001,
        "requirement_evidence_ids": ["identity-001"],
    }
    request = {
        "id": "identity-001",
        "claim": "upstream requires uid 10001",
        "source": "https://github.com/acme/example/blob/v1.2.3/Dockerfile",
        "excerpts": ["USER 10001"],
    }

    with pytest.raises(
        AgentRuntimeError,
        match="identity_decision mode must be dynamic or reuse_existing",
    ):
        _run_image_creator(tmp_path, decision, [request])


def test_image_contract_rejects_numeric_reused_identity(tmp_path):
    from scripts.lib.agent_runtime import AgentRuntimeError

    with pytest.raises(AgentRuntimeError, match="reuse_existing identity.*null"):
        _run_image_creator(
            tmp_path,
            {
                "mode": "reuse_existing",
                "user": "example",
                "group": "example",
                "uid": 10001,
                "gid": 10001,
            },
        )


def test_image_contract_rejects_variable_identity_names(tmp_path):
    from scripts.lib.agent_runtime import AgentRuntimeError

    with pytest.raises(AgentRuntimeError, match="literal Linux name"):
        _run_image_creator(
            tmp_path,
            {
                "mode": "reuse_existing",
                "user": "${APP_UID}",
                "group": "${APP_GID}",
                "uid": None,
                "gid": None,
            },
        )


def test_testcase_contract_leaves_unknown_evidence_id_for_qa(tmp_path):
    result = _run_testcase_creator(
        tmp_path,
        _testcase_creator_payload(
            command_evidence=[
                {
                    "command": "redis-cli EXISTS key",
                    "semantics": "returns whether the key exists",
                    "evidence_id": "command-missing",
                }
            ]
        ),
    )

    assert result.payload["command_evidence"][0]["evidence_id"] == "command-missing"


def test_testcase_contract_leaves_too_many_evidence_entries_for_qa(tmp_path):
    evidence = [
        {
            "id": f"evidence-{index}",
            "claim": "one bounded claim",
            "source": (
                "https://github.com/acme/example/blob/v1.2.3/README.md"
            ),
            "excerpts": [f"claim {index}"],
        }
        for index in range(13)
    ]

    result = _run_testcase_creator(
        tmp_path,
        _testcase_creator_payload(evidence=evidence),
    )

    assert len(result.payload["evidence"]) == 13


def test_testcase_contract_leaves_oversized_evidence_for_qa(tmp_path):
    result = _run_testcase_creator(
        tmp_path,
        _testcase_creator_payload(evidence=[
            {
                "id": "too-large",
                "claim": "x" * 4001,
                "source": (
                    "https://github.com/acme/example/blob/"
                    "v1.2.3/README.md"
                ),
                "excerpts": ["claim"],
            }
        ]),
    )

    assert len(result.payload["evidence"][0]["claim"]) == 4001


def _identity_decision():
    return {
        "mode": "dynamic",
        "user": "kylin",
        "group": "kylin",
        "uid": None,
        "gid": None,
        "requirement_evidence_ids": [],
    }


def test_a_timeout_is_not_turned_into_success_by_partial_output(tmp_path):
    """Only a normally completed Agent response can satisfy the contract."""
    from scripts.lib.agent_runtime import AgentTimeoutError, run_agent

    executable = _executable(tmp_path)
    workspace = tmp_path / "target"
    workspace.mkdir()
    payload = {
        "success": True,
        "files_created": ["Bigdata/kylin/meta.yml"],
        "identity_decision": _identity_decision(),
    }
    stream = "\n".join(
        (
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {"tool": "write", "state": {"status": "completed"}},
                }
            ),
            json.dumps({"type": "text", "part": {"text": json.dumps(payload)}}),
        )
    )
    runner = SequenceRunner([_completed(returncode=124, stdout=stream)])

    with pytest.raises(AgentTimeoutError, match="timed out"):
        run_agent(
            executable=executable,
            role="image_creator",
            prompt="Create the image files.",
            workspace=workspace,
            api_key="deepseek-secret",
            required_keys=("success", "files_created"),
            runner=runner,
        )

    assert len(runner.calls) == 1


def test_scratch_growth_past_the_limit_stops_the_agent(
    tmp_path,
    capsys,
    monkeypatch,
):
    """Run 30872642022 spent most of its budget on avoidable downloads.

    The prompt discourages them, while the Harness independently caps scratch
    growth so a runaway download cannot consume the Runner indefinitely.
    """
    from scripts.lib import agent_runtime, progress

    monkeypatch.setattr(agent_runtime, "_SCRATCH_LIMIT_MB", 1)
    monkeypatch.setattr(progress, "_WATCHDOG_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(progress, "_KILL_GRACE_SECONDS", 0.5)
    executable = tmp_path / "opencode"
    executable.write_text(
        "#!/bin/sh\n"
        'dd if=/dev/zero of="${OE_AGENT_SCRATCH}/blob" '
        "bs=1048576 count=4 2>/dev/null\n"
        "sleep 30\n"
    )
    executable.chmod(0o755)
    workspace = tmp_path / "target"
    workspace.mkdir()

    with pytest.raises(agent_runtime.AgentTimeoutError):
        agent_runtime.run_agent(
            executable=executable,
            role="image_creator",
            prompt="Create the image files.",
            workspace=workspace,
            api_key="deepseek-secret",
            required_keys=("success", "files_created"),
            timeout=30,
        )

    output = capsys.readouterr().out
    assert "ABORT reason=scratch_over_limit" in output
    assert "limit=1MB" in output


def test_unconfirmed_facts_are_accepted_only_as_a_list(tmp_path):
    """Recording what could not be confirmed has to be cheaper than retrying.

    The field stays optional so a payload that omits it is still valid, but a
    malformed one cannot pass as evidence of nothing being assumed.
    """
    from scripts.lib.agent_runtime import AgentRuntimeError, validate_agent_payload

    payload = {
        "success": True,
        "files_created": ["Bigdata/kylin/meta.yml"],
        "assumptions": [
            {
                "claim": "the archive top level directory is apache-kylin-5.0.3-bin",
                "reason": "the release archive was not downloaded",
                "verified_by": "native_build",
            }
        ],
    }
    validate_agent_payload(payload, required_keys=("success", "files_created"))

    with pytest.raises(AgentRuntimeError, match="assumptions must be a list"):
        validate_agent_payload(
            {**payload, "assumptions": "none"},
            required_keys=("success", "files_created"),
        )
