import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "harness" / "phase1.py"
AGENT_CLI = ROOT / "scripts" / "harness" / "run.py"
FLOW_CLI = ROOT / "scripts" / "harness" / "flow.py"
GATE_CLI = ROOT / "scripts" / "harness" / "gate_diff.py"
ARTIFACT_CLI = ROOT / "scripts" / "utils" / "artifacts.py"
BASE_SHA = "1d49c0858d8d8152acb1bd3caf5cd862b091160f"


def _run(*args):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _run_agent_harness(*args):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(AGENT_CLI), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _run_script(script, *args):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _write_candidate_payload(root):
    (root / "reports").mkdir(parents=True)
    (root / "changes.patch").write_text("diff --git a/a b/a\n")
    (root / "reports" / "x86_64.json").write_text('{"status":"passed"}\n')
    (root / "reports" / "aarch64.json").write_text('{"status":"passed"}\n')
    (root / "reports" / "gates.json").write_text('{"status":"passed"}\n')


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _upstream(tmp_path):
    repo = tmp_path / "upstream"
    subprocess.run(
        ["git", "init", "-b", "master", str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.com")
    (repo / "README.md").write_text("upstream\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_task_spec_command_writes_normalized_contract(tmp_path):
    output = tmp_path / "task-spec.json"

    result = _run(
        "task-spec",
        "--app",
        "Kvrocks",
        "--version",
        "2.16.0",
        "--os-version",
        "24.03-LTS-SP4",
        "--domain",
        "database",
        "--source-url",
        "https://github.com/apache/kvrocks",
        "--output",
        str(output),
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["task_id"].startswith("new-image-database-kvrocks")
    assert json.loads(output.read_text())["app"] == "kvrocks"


def test_task_spec_command_fails_without_writing_unsafe_input(tmp_path):
    output = tmp_path / "task-spec.json"

    result = _run(
        "task-spec",
        "--app",
        "../kvrocks",
        "--version",
        "2.16.0",
        "--os-version",
        "24.03-lts-sp4",
        "--domain",
        "Database",
        "--source-url",
        "https://github.com/apache/kvrocks",
        "--output",
        str(output),
    )

    assert result.returncode == 2
    assert "app:" in result.stderr
    assert not output.exists()


def test_delivery_config_command_reports_zero_write_validate_only(tmp_path):
    output = tmp_path / "delivery.json"

    result = _run(
        "delivery-config",
        "--environment",
        "test",
        "--delivery-mode",
        "validate_only",
        "--target-repo",
        "openeuler/openeuler-docker-images",
        "--push-repo",
        "qq_42020325/openeuler-docker-images",
        "--target-branch",
        "master",
        "--output",
        str(output),
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(output.read_text())
    assert summary["allows_branch_push"] is False
    assert summary["allows_pr_create"] is False
    assert summary["duplicate_pr_guard_enabled"] is False


def test_candidate_commands_create_then_verify_for_same_base(tmp_path):
    task_spec = tmp_path / "input-task.json"
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    _write_candidate_payload(candidate)
    task_spec.write_text(
        json.dumps(
            {
                "app": "kvrocks",
                "version": "2.16.0",
                "os_version": "24.03-lts-sp4",
                "domain": "Database",
                "source_url": "https://github.com/apache/kvrocks",
                "scenario": "new-image",
            }
        )
    )

    created = _run(
        "candidate-create",
        "--candidate-dir",
        str(candidate),
        "--task-spec",
        str(task_spec),
        "--base-sha",
        BASE_SHA,
        "--validated-run-id",
        "123456",
    )
    verified = _run(
        "candidate-verify",
        "--candidate-dir",
        str(candidate),
        "--expected-run-id",
        "123456",
        "--current-base-sha",
        BASE_SHA,
    )

    assert created.returncode == 0, created.stderr
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["promotion_action"] == "reuse"


def test_candidate_verify_reports_revalidation_for_changed_base(tmp_path):
    task_spec = tmp_path / "input-task.json"
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    _write_candidate_payload(candidate)
    task_spec.write_text(
        json.dumps(
            {
                "app": "kvrocks",
                "version": "2.16.0",
                "os_version": "24.03-lts-sp4",
                "domain": "Database",
                "source_url": "https://github.com/apache/kvrocks",
            }
        )
    )
    created = _run(
        "candidate-create",
        "--candidate-dir",
        str(candidate),
        "--task-spec",
        str(task_spec),
        "--base-sha",
        BASE_SHA,
        "--validated-run-id",
        "123456",
    )

    verified = _run(
        "candidate-verify",
        "--candidate-dir",
        str(candidate),
        "--expected-run-id",
        "123456",
        "--current-base-sha",
        "a" * 40,
    )

    assert created.returncode == 0, created.stderr
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["promotion_action"] == "revalidate"


def test_target_workspace_commands_clone_create_and_replay_patch(tmp_path):
    upstream = _upstream(tmp_path)
    base_sha = _git(upstream, "rev-parse", "HEAD")
    generated = tmp_path / "generated"
    patch = tmp_path / "generation.patch"

    cloned = _run(
        "target-clone",
        "--source",
        str(upstream),
        "--destination",
        str(generated),
        "--branch",
        "master",
    )
    assert cloned.returncode == 0, cloned.stderr
    assert json.loads(cloned.stdout)["base_sha"] == base_sha

    (generated / "Database").mkdir()
    (generated / "Database" / "new-file").write_text("candidate\n")
    created = _run(
        "target-create-patch",
        "--workspace",
        str(generated),
        "--branch",
        "master",
        "--base-sha",
        base_sha,
        "--output",
        str(patch),
    )
    assert created.returncode == 0, created.stderr
    assert patch.stat().st_size > 0

    replay = tmp_path / "replay"
    exact_clone = _run(
        "target-clone",
        "--source",
        str(upstream),
        "--destination",
        str(replay),
        "--branch",
        "master",
        "--expected-sha",
        base_sha,
    )
    applied = _run(
        "target-apply-patch",
        "--workspace",
        str(replay),
        "--branch",
        "master",
        "--base-sha",
        base_sha,
        "--patch",
        str(patch),
    )

    assert exact_clone.returncode == 0, exact_clone.stderr
    assert applied.returncode == 0, applied.stderr
    assert (replay / "Database" / "new-file").read_text() == "candidate\n"


def test_pipeline_stage_commands_are_exposed():
    for command in (
        "fork-deliver",
        "issue-contract-test",
    ):
        result = _run(command, "--help")
        assert result.returncode == 0, f"{command}: {result.stderr}"

    for command in (
        "phase1-generate",
        "phase1-native-repair",
        "phase1-native-validate",
    ):
        result = _run_agent_harness(command, "--help")
        assert result.returncode == 0, f"{command}: {result.stderr}"

    for script, command in (
        (GATE_CLI, "task-contract"),
        (ARTIFACT_CLI, "aggregate-native"),
    ):
        result = _run_script(script, command, "--help")
        assert result.returncode == 0, f"{command}: {result.stderr}"


def test_flow_is_the_public_entry_for_agent_and_native_stages():
    for command in (
        "phase1-generate",
        "phase1-smoke-generate",
        "phase1-native-smoke",
        "phase1-native-repair",
        "phase1-native-validate",
    ):
        result = _run_script(FLOW_CLI, command, "--help")
        assert result.returncode == 0, f"{command}: {result.stderr}"
        assert "--task-spec" in result.stdout
        if command == "phase1-smoke-generate":
            assert "--opencode" not in result.stdout


def test_fork_delivery_reads_token_only_from_environment():
    result = _run("fork-deliver", "--help")

    assert result.returncode == 0, result.stderr
    assert "--token" not in result.stdout
    assert "GITCODE_TOKEN" in result.stdout


def test_issue_contract_test_is_explicit_and_reads_environment_token():
    result = _run("issue-contract-test", "--help")

    assert result.returncode == 0, result.stderr
    assert "--token" not in result.stdout
    assert "GITCODE_TOKEN" in result.stdout
    assert "create, update, comment and close" in result.stdout


def test_shared_agent_cli_reports_contract_errors_without_traceback(tmp_path):
    task = tmp_path / "task.json"
    task.write_text("{}")
    env = os.environ.copy()
    env.pop("DEEPSEEK_API_KEY", None)
    result = subprocess.run(
        [
            sys.executable,
            str(AGENT_CLI),
            "phase1-generate",
            "--workspace",
            str(tmp_path),
            "--task-spec",
            str(task),
            "--base-sha",
            "1" * 40,
            "--report-dir",
            str(tmp_path / "reports"),
            "--opencode",
            str(tmp_path / "opencode"),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "DEEPSEEK_API_KEY is required" in result.stderr
    assert "Traceback" not in result.stderr
