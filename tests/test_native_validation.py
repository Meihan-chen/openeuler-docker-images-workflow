import json
import subprocess
import xml.etree.ElementTree as ET

import pytest


class DockerRunner:
    def __init__(
        self,
        *,
        fail_build=False,
        failure_text="source compilation failed",
    ):
        self.fail_build = fail_build
        self.failure_text = failure_text
        self.builders = set()
        self.calls = []

    def __call__(self, command, cwd, env, timeout):
        command = list(command)
        self.calls.append(
            {
                "command": command,
                "cwd": cwd,
                "env": dict(env),
                "timeout": timeout,
            }
        )
        if command[:3] == ["docker", "buildx", "inspect"]:
            present = command[3] in self.builders
            return subprocess.CompletedProcess(
                command, 0 if present else 1, "", ""
            )
        if command[:3] == ["docker", "buildx", "create"]:
            self.builders.add(command[command.index("--name") + 1])
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:4] == ["docker", "buildx", "rm", "--force"]:
            self.builders.discard(command[4])
            return subprocess.CompletedProcess(command, 0, "", "")
        if self.fail_build and "build" in command:
            return subprocess.CompletedProcess(
                command, 1, "", self.failure_text
            )
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, "sha256:image-id\n", "")
        if "PING" in command:
            return subprocess.CompletedProcess(command, 0, "PONG\n", "")
        if "GET" in command:
            return subprocess.CompletedProcess(command, 0, "run-123456\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")


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


def _workspace(tmp_path):
    workspace = tmp_path / "target"
    image = workspace / "Database" / "kvrocks" / "2.16.0" / "24.03-lts-sp4"
    tests = workspace / "Database" / "kvrocks" / "tests"
    image.mkdir(parents=True)
    tests.mkdir(parents=True)
    (image / "Dockerfile").write_text("FROM scratch\n")
    (tests / "goss.yaml").write_text("{}\n")
    (tests / "goss_wait.yaml").write_text("{}\n")
    (tests / "test.sh").write_text("#!/bin/bash\n")
    (tests / "test.sh").chmod(0o755)
    return workspace


def _tools(tmp_path):
    dgoss = tmp_path / "dgoss"
    goss = tmp_path / "goss"
    dgoss.write_text("#!/bin/sh\n")
    goss.write_text("#!/bin/sh\n")
    dgoss.chmod(0o755)
    goss.chmod(0o755)
    return dgoss, goss


def test_native_validation_uses_dedicated_builder_and_full_runtime_checks(
    tmp_path,
    capsys,
):
    from scripts.lib.native_validation import validate_native_image

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)
    report_path = tmp_path / "reports" / "x86_64.json"
    junit_path = tmp_path / "reports" / "x86_64.junit.xml"
    runner = DockerRunner()

    report = validate_native_image(
        workspace=workspace,
        task=_task(),
        architecture="x86_64",
        run_id="123456",
        dgoss=dgoss,
        goss=goss,
        report_path=report_path,
        junit_path=junit_path,
        runner=runner,
        sleep=lambda _: None,
    )

    assert report["status"] == "passed"
    assert report["architecture"] == "x86_64"
    assert report["platform"] == "linux/amd64"
    assert report["image_id"] == "sha256:image-id"
    assert set(report["environment"]) == {
        "test_time",
        "Model",
        "architecture",
        "kernel",
        "os",
        "cpu_model",
        "cpu_cores",
        "software_name",
        "software_version",
        "python_version",
        "numpy_version",
    }
    assert report["environment"]["architecture"] == "x86_64"
    assert report["environment"]["software_version"] == "2.16.0"
    assert json.loads(report_path.read_text()) == report
    suite = ET.parse(junit_path).getroot()
    assert suite.attrib["failures"] == "0"

    commands = [call["command"] for call in runner.calls]
    flattened = "\n".join(" ".join(command) for command in commands)
    assert "docker buildx create" in flattened
    assert "--driver docker-container" in flattened
    assert "docker buildx build" in flattened
    assert "--platform linux/amd64" in flattened
    dgoss_call = runner.calls[
        [command[0] for command in commands].index(str(dgoss))
    ]
    assert str(dgoss) in dgoss_call["command"][0]
    assert dgoss_call["env"]["GOSS_FILES_PATH"] == str(
        workspace / "Database" / "kvrocks" / "tests"
    )
    assert dgoss_call["env"]["GOSS_FILE"] == "goss.yaml"
    assert dgoss_call["env"]["GOSS_WAIT_OPTS"] == "-r 30s -s 1s"
    assert "GOSS_WAIT_FILE" not in dgoss_call["env"]
    assert dgoss_call["command"][-3:] == [
        "--env",
        "EXPECTED_VERSION=2.16.0",
        "oe-autopilot/kvrocks:2.16.0-123456-x86-64",
    ]
    assert " SET oe-e2e-persistence run-123456" in flattened
    assert " GET oe-e2e-persistence" in flattened
    assert "docker volume rm" in flattened
    assert "docker image rm" in flattened
    assert "system prune" not in flattened
    assert "setup-qemu" not in flattened
    # The builder carries this run's layer cache, so it must survive until
    # release_run_builders runs; --use would also leak it as the runner-wide
    # default once it is no longer torn down here.
    assert "docker buildx rm" not in flattened
    assert "--use" not in flattened
    output = capsys.readouterr().out
    markers = [
        "[flow][native:x86_64] START validation",
        "[flow][native:x86_64] START build",
        "[flow][native:x86_64] PASS build",
        "[flow][native:x86_64] START dgoss",
        "[flow][native:x86_64] PASS dgoss",
        "[flow][native:x86_64] START persistence",
        "[flow][native:x86_64] PASS persistence",
        "[flow][native:x86_64] PASS validation",
    ]
    positions = [output.index(marker) for marker in markers]
    assert positions == sorted(positions)


def test_native_validation_failure_still_cleans_exact_resources(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)
    report_path = tmp_path / "reports" / "aarch64.json"
    junit_path = tmp_path / "reports" / "aarch64.junit.xml"
    runner = DockerRunner(fail_build=True)

    with pytest.raises(NativeValidationError, match="source compilation failed"):
        validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture="aarch64",
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=report_path,
            junit_path=junit_path,
            runner=runner,
            sleep=lambda _: None,
        )

    report = json.loads(report_path.read_text())
    assert report["status"] == "failed"
    assert report["architecture"] == "aarch64"
    suite = ET.parse(junit_path).getroot()
    assert suite.attrib["failures"] == "1"
    flattened = "\n".join(
        " ".join(call["command"]) for call in runner.calls
    )
    assert "docker volume rm" in flattened
    assert "docker image rm" in flattened
    assert "system prune" not in flattened
    # A failed round is exactly when the next round needs the cached builder.
    assert "docker buildx rm" not in flattened


def test_native_validation_failure_keeps_root_cause_at_end_of_long_log(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)
    report_path = tmp_path / "reports" / "x86_64.json"
    root_cause = "groupadd: GID '999' already exists"
    runner = DockerRunner(
        fail_build=True,
        failure_text=("package progress\n" * 500) + root_cause,
    )

    with pytest.raises(NativeValidationError, match="GID '999'"):
        validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=report_path,
            junit_path=tmp_path / "reports" / "x86_64.junit.xml",
            runner=runner,
            sleep=lambda _: None,
        )

    failure = json.loads(report_path.read_text())["failure"]
    assert root_cause in failure
    assert len(failure) <= 4000


def test_native_pipeline_smoke_builds_and_runs_dgoss_without_ai(
    tmp_path,
):
    from scripts.lib.native_validation import validate_native_smoke

    workspace = tmp_path / "target"
    workspace.mkdir()
    dgoss, goss = _tools(tmp_path)
    runner = DockerRunner()
    report_path = tmp_path / "reports" / "x86_64.json"
    junit_path = tmp_path / "reports" / "x86_64.junit.xml"
    repair_dir = tmp_path / "reports" / "agents"

    report = validate_native_smoke(
        workspace=workspace,
        task=_task(),
        architecture="x86_64",
        run_id="123456",
        dgoss=dgoss,
        goss=goss,
        report_path=report_path,
        junit_path=junit_path,
        repair_report_dir=repair_dir,
        runner=runner,
    )

    assert report["status"] == "passed"
    assert report["checks"] == {
        "native_build": True,
        "dgoss": True,
    }
    commands = "\n".join(
        " ".join(call["command"]) for call in runner.calls
    )
    assert "docker buildx build" in commands
    assert str(dgoss) in commands
    assert "docker image inspect" in commands
    assert "docker image rm" in commands
    assert "docker buildx rm" not in commands
    assert "docker exec" not in commands
    dgoss_call = next(
        call for call in runner.calls if call["command"][0] == str(dgoss)
    )
    assert dgoss_call["env"]["GOSS_FILES_PATH"].endswith(
        "pipeline-smoke-context"
    )
    assert dgoss_call["env"]["GOSS_FILE"] == "goss.yaml"
    assert (
        repair_dir / "native-repair-x86_64.json"
    ).is_file()


def test_repeated_validation_reuses_the_run_builder_and_its_layer_cache(
    tmp_path,
):
    from scripts.lib.native_validation import validate_native_image

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)
    runner = DockerRunner()

    for attempt in range(2):
        validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=tmp_path / f"reports/{attempt}/x86_64.json",
            junit_path=tmp_path / f"reports/{attempt}/x86_64.junit.xml",
            runner=runner,
            sleep=lambda _: None,
        )

    commands = [call["command"] for call in runner.calls]
    creates = [command for command in commands if command[:3] == [
        "docker", "buildx", "create",
    ]]
    inspects = [command for command in commands if command[:3] == [
        "docker", "buildx", "inspect",
    ]]
    # A repair round re-enters validation; recreating the builder would throw
    # away the cached builder stage and rebuild from source every round.
    assert len(inspects) == 2
    assert len(creates) == 1
    assert creates[0][creates[0].index("--name") + 1] == (
        "oe-e2e-123456-x86-64-builder"
    )


def test_release_run_builders_frees_exactly_this_run_on_one_architecture(
    tmp_path,
):
    from scripts.lib.native_validation import release_run_builders

    runner = DockerRunner()

    report = release_run_builders(
        run_id="123456",
        architecture="aarch64",
        workspace=tmp_path,
        runner=runner,
    )

    removed = [
        call["command"][4]
        for call in runner.calls
        if call["command"][:4] == ["docker", "buildx", "rm", "--force"]
    ]
    assert removed == [
        "oe-e2e-123456-aarch64-builder",
        "oe-smoke-123456-aarch64-builder",
    ]
    assert report["released_builders"] == removed
    assert report["status"] == "passed"


@pytest.mark.parametrize("run_id", ["", "0", "abc", "12x"])
def test_release_run_builders_rejects_an_unusable_run_id(tmp_path, run_id):
    from scripts.lib.native_validation import (
        NativeValidationError,
        release_run_builders,
    )

    with pytest.raises(NativeValidationError, match="run_id"):
        release_run_builders(
            run_id=run_id,
            architecture="x86_64",
            workspace=tmp_path,
            runner=DockerRunner(),
        )


@pytest.mark.parametrize("architecture", ["amd64", "arm64", "../x86_64"])
def test_native_validation_rejects_non_runner_architecture_names(
    tmp_path, architecture
):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    workspace = _workspace(tmp_path)
    dgoss, goss = _tools(tmp_path)
    runner = DockerRunner()

    with pytest.raises(NativeValidationError, match="architecture"):
        validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture=architecture,
            run_id="123456",
            dgoss=dgoss,
            goss=goss,
            report_path=tmp_path / "report.json",
            junit_path=tmp_path / "report.xml",
            runner=runner,
        )

    assert runner.calls == []
