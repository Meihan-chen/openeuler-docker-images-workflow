import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

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


def _git_init(workspace):
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    for key, value in (("user.email", "t@example.com"), ("user.name", "T")):
        subprocess.run(
            ["git", "-C", str(workspace), "config", key, value], check=True
        )
    (workspace / ".keep").write_text("base\n")
    subprocess.run(
        ["git", "-C", str(workspace), "add", "--", ".keep"], check=True
    )
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-qm", "base"], check=True
    )
    return workspace


def _workspace(tmp_path):
    workspace = tmp_path / "target"
    image = workspace / "Database" / "kvrocks" / "2.16.0" / "24.03-lts-sp4"
    tests = workspace / "Database" / "kvrocks" / "tests"
    image.mkdir(parents=True)
    tests.mkdir(parents=True)
    (image / "Dockerfile").write_text("FROM scratch\n")
    test_sh = tests / "test.sh"
    test_sh.write_text("#!/bin/bash\nexit 0\n")
    test_sh.chmod(0o755)
    return _git_init(workspace)


def test_infrastructure_failure_evidence_preserves_clone_error(tmp_path):
    from scripts.lib.native_validation import write_infrastructure_failure_evidence

    report_path = tmp_path / "round" / "aarch64.json"
    junit_path = tmp_path / "round" / "aarch64.junit.xml"
    failure = "error: RPC failed; curl 18 transfer closed\nfatal: early EOF"

    report = write_infrastructure_failure_evidence(
        task=_task(),
        architecture="aarch64",
        failed_stage="target_clone",
        failure=failure,
        report_path=report_path,
        junit_path=junit_path,
        attempts=2,
    )

    assert json.loads(report_path.read_text()) == report
    assert report["checks"] == {
        "native_build": None,
        "runtime_test": None,
    }
    assert report["failure_details"] == {"attempts": 2, "retryable": True}
    suite = ET.parse(junit_path).getroot()
    assert suite.attrib["failures"] == "1"
    assert failure in suite.find("testcase/failure").text


class RuntimeStateRunner:
    """Small Docker boundary fake for the runtime state-machine contract."""

    def __init__(
        self,
        *,
        healthcheck=None,
        exposed_ports=None,
        wait_status="READY_HEALTH",
        states=None,
        test_returncode=0,
        create_returncode=0,
        start_returncode=0,
        image_inspect_returncode=0,
        wait_returncode=None,
        wait_exception=None,
        fail_build=False,
        failure_text="source compilation failed",
        container_logs="",
        container_logs_returncode=0,
        container_state="exited 1 application failed",
        container_probe="",
        container_probe_returncode=0,
        docker_api_version="1.44",
        docker_api_returncode=0,
    ):
        self.healthcheck = healthcheck
        self.exposed_ports = exposed_ports or {}
        self.wait_status = wait_status
        self.states = list(
            states
            or [
                {
                    "Status": "running",
                    "ExitCode": 0,
                    "OOMKilled": False,
                    "Error": "",
                    "Health": {"Status": "healthy"},
                },
                {
                    "Status": "running",
                    "ExitCode": 0,
                    "OOMKilled": False,
                    "Error": "",
                    "Health": {"Status": "healthy"},
                },
            ]
        )
        self.test_returncode = test_returncode
        self.create_returncode = create_returncode
        self.start_returncode = start_returncode
        self.image_inspect_returncode = image_inspect_returncode
        self.wait_returncode = wait_returncode
        self.wait_exception = wait_exception
        self.fail_build = fail_build
        self.failure_text = failure_text
        self.container_logs = container_logs
        self.container_logs_returncode = container_logs_returncode
        self.container_state = container_state
        self.container_probe = container_probe
        self.container_probe_returncode = container_probe_returncode
        self.docker_api_version = docker_api_version
        self.docker_api_returncode = docker_api_returncode
        self.builders = set()
        self.calls = []

    def __call__(self, command, cwd, env, timeout):
        command = list(command)
        self.calls.append(command)
        if command[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(
                command,
                self.container_logs_returncode,
                self.container_logs,
                "",
            )
        if command[:3] == ["docker", "buildx", "inspect"]:
            return subprocess.CompletedProcess(
                command, 0 if command[3] in self.builders else 1, "", ""
            )
        if command[:3] == ["docker", "buildx", "create"]:
            self.builders.add(command[command.index("--name") + 1])
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["docker", "buildx", "ls"]:
            output = "\n".join(sorted(self.builders))
            return subprocess.CompletedProcess(command, 0, output, "")
        if command[:4] == ["docker", "buildx", "rm", "--force"]:
            self.builders.discard(command[4])
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["docker", "version", "--format"]:
            return subprocess.CompletedProcess(
                command,
                self.docker_api_returncode,
                self.docker_api_version + "\n"
                if self.docker_api_returncode == 0
                else "",
                "cannot query Docker API" if self.docker_api_returncode else "",
            )
        if self.fail_build and command[:3] == ["docker", "buildx", "build"]:
            return subprocess.CompletedProcess(
                command, 1, "", self.failure_text
            )
        if command[:3] == ["docker", "image", "inspect"]:
            if "--format" in command:
                return subprocess.CompletedProcess(
                    command, 0, "sha256:candidate\n", ""
                )
            if self.image_inspect_returncode:
                return subprocess.CompletedProcess(
                    command,
                    self.image_inspect_returncode,
                    "",
                    "cannot inspect image",
                )
            config = {
                "Healthcheck": self.healthcheck,
                "ExposedPorts": self.exposed_ports,
            }
            return subprocess.CompletedProcess(
                command, 0, json.dumps([{"Config": config}]), ""
            )
        if command[:3] == ["docker", "inspect", "--format"]:
            if command[3] != "{{json .State}}":
                return subprocess.CompletedProcess(
                    command, 0, self.container_state + "\n", ""
                )
            state = self.states.pop(0)
            if state is None:
                return subprocess.CompletedProcess(
                    command, 1, "", "docker inspect unavailable"
                )
            return subprocess.CompletedProcess(command, 0, json.dumps(state), "")
        if command[0].endswith("wait_ready.sh"):
            if self.wait_exception is not None:
                raise self.wait_exception
            returncode = (
                self.wait_returncode
                if self.wait_returncode is not None
                else {"PROBE_TIMEOUT": 2, "RUNTIME_ERROR": 3}.get(
                    self.wait_status, 0
                )
            )
            return subprocess.CompletedProcess(
                command, returncode, self.wait_status + "\n", ""
            )
        if command[:2] == ["docker", "create"]:
            return subprocess.CompletedProcess(
                command,
                self.create_returncode,
                "",
                "cannot create container" if self.create_returncode else "",
            )
        if command[:2] == ["docker", "start"]:
            return subprocess.CompletedProcess(
                command,
                self.start_returncode,
                "",
                "cannot start container" if self.start_returncode else "",
            )
        if command[:2] == ["docker", "exec"]:
            if "### processes" in command[-1]:
                return subprocess.CompletedProcess(
                    command,
                    self.container_probe_returncode,
                    self.container_probe,
                    "",
                )
            return subprocess.CompletedProcess(
                command, self.test_returncode, "test output\n", ""
            )
        if command[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(
                command, self.test_returncode, "test output\n", ""
            )
        return subprocess.CompletedProcess(command, 0, "", "")


def _runtime_paths(tmp_path):
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    waiter = tmp_path / "wait_ready.sh"
    waiter.write_text("#!/bin/bash\n")
    waiter.chmod(0o755)
    return tests_root, waiter


def test_runtime_health_api_144_uses_temporary_fast_health_and_tests_once(tmp_path):
    from scripts.lib.native_validation import _run_runtime_test

    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    waiter = tmp_path / "wait_ready.sh"
    waiter.write_text("#!/bin/bash\n")
    waiter.chmod(0o755)
    runner = RuntimeStateRunner(
        healthcheck={"Test": ["CMD", "true"]},
        wait_status="READY_HEALTH",
    )

    result = _run_runtime_test(
        runner,
        workspace=tmp_path,
        image_id="sha256:candidate",
        tests_root=tests_root,
        version="1.2.3",
        container="candidate-default",
        test_container="candidate-test",
        run_id="123",
        waiter=waiter,
        wait_timeout=90,
    )

    assert result == {
        "wait_status": "READY_HEALTH",
        "carrier": "default",
        "state": {
            "Status": "running",
            "ExitCode": 0,
            "OOMKilled": False,
            "Error": "",
            "Health": {"Status": "healthy"},
        },
    }
    create = next(call for call in runner.calls if call[:2] == ["docker", "create"])
    assert ["docker", "version", "--format", "{{.Client.APIVersion}}"] in runner.calls
    assert "--health-interval=1s" in create
    assert "--health-start-interval=1s" in create
    assert create[-1] == "sha256:candidate"
    wait = next(call for call in runner.calls if call[0] == str(waiter))
    assert wait == [str(waiter), "candidate-default", "90", "health"]
    test_calls = [
        call
        for call in runner.calls
        if "/opt/oe-tests/test.sh" in call
    ]
    assert test_calls == [
        [
            "docker",
            "exec",
            "--env",
            "EXPECTED_VERSION=1.2.3",
            "candidate-default",
            "/bin/bash",
            "/opt/oe-tests/test.sh",
        ]
    ]


def test_runtime_health_api_143_omits_start_interval(tmp_path):
    from scripts.lib.native_validation import _run_runtime_test

    tests_root, waiter = _runtime_paths(tmp_path)
    runner = RuntimeStateRunner(
        healthcheck={"Test": ["CMD", "true"]},
        docker_api_version="1.43",
    )

    _run_runtime_test(
        runner,
        workspace=tmp_path,
        image_id="sha256:candidate",
        tests_root=tests_root,
        version="1.2.3",
        container="candidate-default",
        test_container="candidate-test",
        run_id="123",
        waiter=waiter,
    )

    create = next(call for call in runner.calls if call[:2] == ["docker", "create"])
    assert ["docker", "version", "--format", "{{.Client.APIVersion}}"] in runner.calls
    assert "--health-interval=1s" in create
    assert "--health-start-interval=1s" not in create


@pytest.mark.parametrize(
    ("docker_api_version", "docker_api_returncode"),
    (("1.44", 1), ("not-a-version", 0)),
    ids=("query-failed", "unparseable-version"),
)
def test_runtime_health_api_unavailable_omits_start_interval(
    tmp_path,
    docker_api_version,
    docker_api_returncode,
):
    from scripts.lib.native_validation import _run_runtime_test

    tests_root, waiter = _runtime_paths(tmp_path)
    runner = RuntimeStateRunner(
        healthcheck={"Test": ["CMD", "true"]},
        docker_api_version=docker_api_version,
        docker_api_returncode=docker_api_returncode,
    )

    _run_runtime_test(
        runner,
        workspace=tmp_path,
        image_id="sha256:candidate",
        tests_root=tests_root,
        version="1.2.3",
        container="candidate-default",
        test_container="candidate-test",
        run_id="123",
        waiter=waiter,
    )

    create = next(call for call in runner.calls if call[:2] == ["docker", "create"])
    assert ["docker", "version", "--format", "{{.Client.APIVersion}}"] in runner.calls
    assert "--health-interval=1s" in create
    assert "--health-start-interval=1s" not in create


@pytest.mark.parametrize("default_exit_code", [0, 17])
def test_runtime_terminal_exit_uses_fresh_test_as_semantic_authority(
    tmp_path,
    default_exit_code,
):
    from scripts.lib.native_validation import _run_runtime_test

    tests_root, waiter = _runtime_paths(tmp_path)
    terminal = {
        "Status": "exited",
        "ExitCode": default_exit_code,
        "OOMKilled": False,
        "Error": "",
    }
    runner = RuntimeStateRunner(
        wait_status="TERMINAL",
        states=[terminal, terminal],
    )

    result = _run_runtime_test(
        runner,
        workspace=tmp_path,
        image_id="sha256:candidate",
        tests_root=tests_root,
        version="1.2.3",
        container="candidate-default",
        test_container="candidate-test",
        run_id="123",
        waiter=waiter,
    )

    assert result["carrier"] == "fresh"
    test_calls = [
        call for call in runner.calls if "/opt/oe-tests/test.sh" in call
    ]
    assert len(test_calls) == 1
    assert test_calls[0][:2] == ["docker", "run"]


def test_runtime_terminal_fresh_test_failure_fails_validation(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        _run_runtime_test,
    )

    tests_root, waiter = _runtime_paths(tmp_path)
    terminal = {
        "Status": "exited",
        "ExitCode": 0,
        "OOMKilled": False,
        "Error": "",
    }
    runner = RuntimeStateRunner(
        wait_status="TERMINAL",
        states=[terminal, terminal],
        test_returncode=12,
    )

    with pytest.raises(NativeValidationError) as raised:
        _run_runtime_test(
            runner,
            workspace=tmp_path,
            image_id="sha256:candidate",
            tests_root=tests_root,
            version="1.2.3",
            container="candidate-default",
            test_container="candidate-test",
            run_id="123",
            waiter=waiter,
        )

    assert [item["stage"] for item in raised.value.details["failures"]] == [
        "test_sh"
    ]


def test_runtime_no_probe_allows_clean_exit_after_test(tmp_path):
    from scripts.lib.native_validation import _run_runtime_test

    tests_root, waiter = _runtime_paths(tmp_path)
    runner = RuntimeStateRunner(
        wait_status="RUNNING_NO_PROBE",
        states=[
            {
                "Status": "running",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
            },
            {
                "Status": "exited",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
            },
        ],
    )

    result = _run_runtime_test(
        runner,
        workspace=tmp_path,
        image_id="sha256:candidate",
        tests_root=tests_root,
        version="1.2.3",
        container="candidate-default",
        test_container="candidate-test",
        run_id="123",
        waiter=waiter,
    )

    assert result["state"]["Status"] == "exited"
    assert result["state"]["ExitCode"] == 0


def test_runtime_health_ready_rejects_post_test_unhealthy_state(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        _run_runtime_test,
    )

    tests_root, waiter = _runtime_paths(tmp_path)
    runner = RuntimeStateRunner(
        healthcheck={"Test": ["CMD", "true"]},
        wait_status="READY_HEALTH",
        states=[
            {
                "Status": "running",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
                "Health": {"Status": "healthy"},
            },
            {
                "Status": "running",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
                "Health": {"Status": "unhealthy"},
            },
        ],
    )

    with pytest.raises(NativeValidationError) as raised:
        _run_runtime_test(
            runner,
            workspace=tmp_path,
            image_id="sha256:candidate",
            tests_root=tests_root,
            version="1.2.3",
            container="candidate-default",
            test_container="candidate-test",
            run_id="123",
            waiter=waiter,
        )

    assert raised.value.details["failures"][-1]["stage"] == "post_inspect"


def test_runtime_does_not_retry_when_container_exits_during_same_container_test(
    tmp_path,
):
    from scripts.lib.native_validation import (
        NativeValidationError,
        _run_runtime_test,
    )

    tests_root, waiter = _runtime_paths(tmp_path)
    runner = RuntimeStateRunner(
        wait_status="RUNNING_NO_PROBE",
        states=[
            {
                "Status": "running",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
            },
            {
                "Status": "exited",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
            },
        ],
        test_returncode=1,
    )

    with pytest.raises(NativeValidationError):
        _run_runtime_test(
            runner,
            workspace=tmp_path,
            image_id="sha256:candidate",
            tests_root=tests_root,
            version="1.2.3",
            container="candidate-default",
            test_container="candidate-test",
            run_id="123",
            waiter=waiter,
        )

    test_calls = [
        call for call in runner.calls if "/opt/oe-tests/test.sh" in call
    ]
    assert len(test_calls) == 1
    assert test_calls[0][:2] == ["docker", "exec"]


def test_runtime_explicit_probe_timeout_tests_once_but_cannot_pass(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        _run_runtime_test,
    )

    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    waiter = tmp_path / "wait_ready.sh"
    waiter.write_text("#!/bin/bash\n")
    waiter.chmod(0o755)
    runner = RuntimeStateRunner(
        healthcheck={"Test": ["CMD-SHELL", "check-ready"]},
        wait_status="PROBE_TIMEOUT",
        states=[
            {
                "Status": "running",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
                "Health": {"Status": "starting"},
            },
            {
                "Status": "running",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
                "Health": {"Status": "starting"},
            },
        ],
    )

    with pytest.raises(NativeValidationError) as raised:
        _run_runtime_test(
            runner,
            workspace=tmp_path,
            image_id="sha256:candidate",
            tests_root=tests_root,
            version="1.2.3",
            container="candidate-default",
            test_container="candidate-test",
            run_id="123",
            waiter=waiter,
            wait_timeout=90,
        )

    assert raised.value.details["failures"][0]["stage"] == "wait_healthcheck"
    assert raised.value.details["wait_status"] == "PROBE_TIMEOUT"
    assert raised.value.details["test_attempted"] is True
    assert len(
        [call for call in runner.calls if "/opt/oe-tests/test.sh" in call]
    ) == 1


@pytest.mark.parametrize(
    ("oom_killed", "state_error"),
    [(True, ""), (False, "failed to create task")],
)
def test_runtime_oom_or_state_error_is_hard_failure_after_test_attempt(
    tmp_path,
    oom_killed,
    state_error,
):
    from scripts.lib.native_validation import (
        NativeValidationError,
        _run_runtime_test,
    )

    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    waiter = tmp_path / "wait_ready.sh"
    waiter.write_text("#!/bin/bash\n")
    waiter.chmod(0o755)
    failed_state = {
        "Status": "exited",
        "ExitCode": 137 if oom_killed else 1,
        "OOMKilled": oom_killed,
        "Error": state_error,
    }
    runner = RuntimeStateRunner(
        healthcheck={"Test": ["CMD", "true"]},
        wait_status="RUNTIME_ERROR",
        states=[failed_state, failed_state],
    )

    with pytest.raises(NativeValidationError) as raised:
        _run_runtime_test(
            runner,
            workspace=tmp_path,
            image_id="sha256:candidate",
            tests_root=tests_root,
            version="1.2.3",
            container="candidate-default",
            test_container="candidate-test",
            run_id="123",
            waiter=waiter,
        )

    stages = [failure["stage"] for failure in raised.value.details["failures"]]
    assert "post_inspect" in stages
    assert len(
        [call for call in runner.calls if "/opt/oe-tests/test.sh" in call]
    ) == 1


def test_runtime_preserves_wait_test_and_post_inspect_failures(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        _run_runtime_test,
    )

    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    waiter = tmp_path / "wait_ready.sh"
    waiter.write_text("#!/bin/bash\n")
    waiter.chmod(0o755)
    runner = RuntimeStateRunner(
        healthcheck={"Test": ["CMD", "true"]},
        wait_status="PROBE_TIMEOUT",
        states=[
            {
                "Status": "running",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
                "Health": {"Status": "starting"},
            },
            None,
        ],
        test_returncode=23,
    )

    with pytest.raises(NativeValidationError) as raised:
        _run_runtime_test(
            runner,
            workspace=tmp_path,
            image_id="sha256:candidate",
            tests_root=tests_root,
            version="1.2.3",
            container="candidate-default",
            test_container="candidate-test",
            run_id="123",
            waiter=waiter,
        )

    assert [
        failure["stage"] for failure in raised.value.details["failures"]
    ] == ["wait_healthcheck", "test_sh", "post_inspect"]


def test_runtime_create_error_still_attempts_one_fresh_test(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        _run_runtime_test,
    )

    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    waiter = tmp_path / "wait_ready.sh"
    waiter.write_text("#!/bin/bash\n")
    waiter.chmod(0o755)
    runner = RuntimeStateRunner(
        healthcheck=None,
        wait_status="RUNTIME_ERROR",
        create_returncode=125,
    )

    with pytest.raises(NativeValidationError) as raised:
        _run_runtime_test(
            runner,
            workspace=tmp_path,
            image_id="sha256:candidate",
            tests_root=tests_root,
            version="1.2.3",
            container="candidate-default",
            test_container="candidate-test",
            run_id="123",
            waiter=waiter,
        )

    assert raised.value.details["failures"][0]["stage"] == "default_start"
    assert raised.value.details["carrier"] == "fresh"
    test_calls = [
        call for call in runner.calls if "/opt/oe-tests/test.sh" in call
    ]
    assert len(test_calls) == 1
    assert test_calls[0][:2] == ["docker", "run"]


def test_runtime_tcp_ready_requires_default_container_to_remain_running(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        _run_runtime_test,
    )

    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    waiter = tmp_path / "wait_ready.sh"
    waiter.write_text("#!/bin/bash\n")
    waiter.chmod(0o755)
    runner = RuntimeStateRunner(
        exposed_ports={"8443/tcp": {}, "8080/tcp": {}, "53/udp": {}},
        wait_status="READY_TCP",
        states=[
            {
                "Status": "running",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
            },
            {
                "Status": "exited",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
            },
        ],
    )

    with pytest.raises(NativeValidationError) as raised:
        _run_runtime_test(
            runner,
            workspace=tmp_path,
            image_id="sha256:candidate",
            tests_root=tests_root,
            version="1.2.3",
            container="candidate-default",
            test_container="candidate-test",
            run_id="123",
            waiter=waiter,
        )

    assert raised.value.details["failures"][-1]["stage"] == "post_inspect"
    wait = next(call for call in runner.calls if call[0] == str(waiter))
    assert wait == [
        str(waiter),
        "candidate-default",
        "90",
        "tcp",
        "8080",
        "8443",
    ]


def test_runtime_no_probe_rejects_nonzero_exit_after_test(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        _run_runtime_test,
    )

    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    waiter = tmp_path / "wait_ready.sh"
    waiter.write_text("#!/bin/bash\n")
    waiter.chmod(0o755)
    runner = RuntimeStateRunner(
        wait_status="RUNNING_NO_PROBE",
        states=[
            {
                "Status": "running",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
            },
            {
                "Status": "exited",
                "ExitCode": 9,
                "OOMKilled": False,
                "Error": "",
            },
        ],
    )

    with pytest.raises(NativeValidationError) as raised:
        _run_runtime_test(
            runner,
            workspace=tmp_path,
            image_id="sha256:candidate",
            tests_root=tests_root,
            version="1.2.3",
            container="candidate-default",
            test_container="candidate-test",
            run_id="123",
            waiter=waiter,
        )

    assert raised.value.details["failures"][-1]["stage"] == "post_inspect"


def test_runtime_image_inspect_error_still_attempts_one_fresh_test(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        _run_runtime_test,
    )

    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    waiter = tmp_path / "wait_ready.sh"
    waiter.write_text("#!/bin/bash\n")
    waiter.chmod(0o755)
    runner = RuntimeStateRunner(image_inspect_returncode=125)

    with pytest.raises(NativeValidationError) as raised:
        _run_runtime_test(
            runner,
            workspace=tmp_path,
            image_id="sha256:candidate",
            tests_root=tests_root,
            version="1.2.3",
            container="candidate-default",
            test_container="candidate-test",
            run_id="123",
            waiter=waiter,
        )

    assert raised.value.details["failures"][0]["stage"] == "default_start"
    assert raised.value.details["carrier"] == "fresh"
    test_calls = [
        call for call in runner.calls if "/opt/oe-tests/test.sh" in call
    ]
    assert len(test_calls) == 1
    assert test_calls[0][:2] == ["docker", "run"]


def test_runtime_waiter_process_timeout_still_tests_once(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        _run_runtime_test,
    )

    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    waiter = tmp_path / "wait_ready.sh"
    waiter.write_text("#!/bin/bash\n")
    waiter.chmod(0o755)
    runner = RuntimeStateRunner(
        healthcheck={"Test": ["CMD", "true"]},
        wait_exception=subprocess.TimeoutExpired([str(waiter)], 100),
    )

    with pytest.raises(NativeValidationError) as raised:
        _run_runtime_test(
            runner,
            workspace=tmp_path,
            image_id="sha256:candidate",
            tests_root=tests_root,
            version="1.2.3",
            container="candidate-default",
            test_container="candidate-test",
            run_id="123",
            waiter=waiter,
        )

    assert raised.value.details["failures"][0]["stage"] == "wait_healthcheck"
    assert len(
        [call for call in runner.calls if "/opt/oe-tests/test.sh" in call]
    ) == 1


def test_runtime_rejects_ready_output_from_failed_waiter_process(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        _run_runtime_test,
    )

    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    waiter = tmp_path / "wait_ready.sh"
    waiter.write_text("#!/bin/bash\n")
    waiter.chmod(0o755)
    runner = RuntimeStateRunner(
        healthcheck={"Test": ["CMD", "true"]},
        wait_status="READY_HEALTH",
        wait_returncode=3,
    )

    with pytest.raises(NativeValidationError) as raised:
        _run_runtime_test(
            runner,
            workspace=tmp_path,
            image_id="sha256:candidate",
            tests_root=tests_root,
            version="1.2.3",
            container="candidate-default",
            test_container="candidate-test",
            run_id="123",
            waiter=waiter,
        )

    assert raised.value.details["failures"][0]["stage"] == "wait_healthcheck"
    assert raised.value.details["wait_status"] == "RUNTIME_ERROR"
    assert len(
        [call for call in runner.calls if "/opt/oe-tests/test.sh" in call]
    ) == 1


def test_native_validation_reports_only_build_and_runtime_test(tmp_path):
    from scripts.lib.native_validation import validate_native_image

    workspace = _workspace(tmp_path)
    runner = RuntimeStateRunner(
        wait_status="RUNNING_NO_PROBE",
        states=[
            {
                "Status": "running",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
            },
            {
                "Status": "running",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
            },
        ],
    )

    report = validate_native_image(
        workspace=workspace,
        task=_task(),
        architecture="x86_64",
        run_id="123456",
        report_path=tmp_path / "reports/x86_64.json",
        junit_path=tmp_path / "reports/x86_64.junit.xml",
        runner=runner,
    )

    assert report["status"] == "passed"
    assert report["checks"] == {
        "native_build": True,
        "runtime_test": True,
    }
    runtime_evidence = report["container_evidence"][
        "oe-e2e-123456-x86-64-runtime"
    ]
    assert runtime_evidence["probe"] == (
        "wait_status=RUNNING_NO_PROBE carrier=default"
    )
    assert runtime_evidence["state"] == "running 0"
    assert "failures" not in report
    assert len(
        [call for call in runner.calls if "/opt/oe-tests/test.sh" in call]
    ) == 1


def test_native_validation_reports_each_runtime_substage_failure(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    workspace = _workspace(tmp_path)
    report_path = tmp_path / "reports/x86_64.json"
    runner = RuntimeStateRunner(
        healthcheck={"Test": ["CMD", "true"]},
        wait_status="PROBE_TIMEOUT",
        states=[
            {
                "Status": "running",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
                "Health": {"Status": "starting"},
            },
            {
                "Status": "running",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
                "Health": {"Status": "starting"},
            },
        ],
        test_returncode=9,
    )

    with pytest.raises(NativeValidationError):
        validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            report_path=report_path,
            junit_path=tmp_path / "reports/x86_64.junit.xml",
            runner=runner,
        )

    report = json.loads(report_path.read_text())
    assert report["checks"] == {
        "native_build": True,
        "runtime_test": False,
    }
    assert [failure["stage"] for failure in report["failures"]] == [
        "wait_healthcheck",
        "test_sh",
    ]
    assert all(
        failure["check"] == "runtime_test" for failure in report["failures"]
    )


def test_native_validation_failure_cleans_only_owned_resources(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    report_path = tmp_path / "reports/aarch64.json"
    runner = RuntimeStateRunner(fail_build=True)

    with pytest.raises(NativeValidationError, match="source compilation failed"):
        validate_native_image(
            workspace=_workspace(tmp_path),
            task=_task(),
            architecture="aarch64",
            run_id="123456",
            report_path=report_path,
            junit_path=tmp_path / "reports/aarch64.junit.xml",
            runner=runner,
        )

    report = json.loads(report_path.read_text())
    assert report["checks"] == {
        "native_build": False,
        "runtime_test": None,
    }
    commands = [" ".join(command) for command in runner.calls]
    assert "docker rm --force oe-e2e-123456-aarch64-runtime" in commands
    assert "docker rm --force oe-e2e-123456-aarch64-runtime-test" in commands
    assert any(command.startswith("docker image rm --force") for command in commands)
    assert not any("system prune" in command for command in commands)
    assert not any(command.startswith("docker buildx rm") for command in commands)


def test_native_build_failure_keeps_both_ends_of_long_output(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    first_error = "CMake Error: could not find libstdc++.a"
    root_cause = "groupadd: GID '999' already exists"
    runner = RuntimeStateRunner(
        fail_build=True,
        failure_text=first_error + "\n" + ("package progress\n" * 500) + root_cause,
    )
    report_path = tmp_path / "reports/x86_64.json"

    with pytest.raises(NativeValidationError, match="GID '999'"):
        validate_native_image(
            workspace=_workspace(tmp_path),
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            report_path=report_path,
            junit_path=tmp_path / "reports/x86_64.junit.xml",
            runner=runner,
        )

    details = json.loads(report_path.read_text())["failure_details"]
    assert first_error in details["stdout_head"]
    assert root_cause in details["stdout_tail"]
    assert details["returncode"] == 1


def test_format_failure_is_recorded_after_runtime_validation_runs(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    def failing_format_check(**_):
        return {
            "status": "failed",
            "kind": "candidate",
            "stage": "execute",
            "commit_sha": "a" * 40,
            "failure": "upstream format check reported 1 failure",
        }

    runner = RuntimeStateRunner(wait_status="RUNNING_NO_PROBE")
    report_path = tmp_path / "reports/aarch64.json"

    with pytest.raises(NativeValidationError, match="format check"):
        validate_native_image(
            workspace=_workspace(tmp_path),
            task=_task(),
            architecture="aarch64",
            run_id="123456",
            report_path=report_path,
            junit_path=tmp_path / "reports/aarch64.junit.xml",
            runner=runner,
            format_validator=failing_format_check,
        )

    report = json.loads(report_path.read_text())
    assert report["failed_stage"] == "upstream_format"
    assert report["checks"] == {
        "native_build": True,
        "runtime_test": True,
    }
    assert any(
        command[:3] == ["docker", "buildx", "build"] for command in runner.calls
    )
    assert len(
        [command for command in runner.calls if "/opt/oe-tests/test.sh" in command]
    ) == 1


def test_repeated_validation_reuses_run_builder_cache(tmp_path):
    from scripts.lib.native_validation import validate_native_image

    workspace = _workspace(tmp_path)
    runner = RuntimeStateRunner(
        wait_status="RUNNING_NO_PROBE",
        states=[
            {
                "Status": "running",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
            }
            for _ in range(4)
        ],
    )

    for attempt in (1, 2):
        validate_native_image(
            workspace=workspace,
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            report_path=tmp_path / f"reports/{attempt}.json",
            junit_path=tmp_path / f"reports/{attempt}.xml",
            runner=runner,
        )

    creates = [
        command
        for command in runner.calls
        if command[:3] == ["docker", "buildx", "create"]
    ]
    assert len(creates) == 1
    assert "--use" not in creates[0]


def test_report_records_digest_of_validated_candidate(tmp_path):
    from scripts.lib.native_validation import (
        validate_native_image,
        validated_patch_digest,
    )

    workspace = _workspace(tmp_path)
    dockerfile = (
        workspace
        / "Database"
        / "kvrocks"
        / "2.16.0"
        / "24.03-lts-sp4"
        / "Dockerfile"
    )
    dockerfile.write_text("FROM scratch\nLABEL candidate=yes\n")
    expected = validated_patch_digest(workspace)

    report = validate_native_image(
        workspace=workspace,
        task=_task(),
        architecture="x86_64",
        run_id="123456",
        report_path=tmp_path / "reports/x86_64.json",
        junit_path=tmp_path / "reports/x86_64.xml",
        runner=RuntimeStateRunner(wait_status="RUNNING_NO_PROBE"),
    )

    assert report["validated_patch_sha256"] == expected


def _failing_runtime_runner(**overrides):
    options = {
        "healthcheck": {"Test": ["CMD", "true"]},
        "wait_status": "PROBE_TIMEOUT",
        "states": [
            {
                "Status": "running",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
                "Health": {"Status": "starting"},
            },
            {
                "Status": "running",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
                "Health": {"Status": "starting"},
            },
        ],
    }
    options.update(overrides)
    return RuntimeStateRunner(**options)


def _run_expected_runtime_failure(tmp_path, runner):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    report_path = tmp_path / "reports/x86_64.json"
    with pytest.raises(NativeValidationError):
        validate_native_image(
            workspace=_workspace(tmp_path),
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            report_path=report_path,
            junit_path=tmp_path / "reports/x86_64.junit.xml",
            runner=runner,
        )
    return report_path, json.loads(report_path.read_text())


def test_runtime_failure_captures_state_logs_and_probe_before_cleanup(tmp_path):
    runner = _failing_runtime_runner(
        container_state="exited 1 application refused to start",
        container_logs="FATAL: cannot bind 0.0.0.0:6666\n",
        container_probe=(
            "### processes\n"
            "UID PID CMD\n"
            "### /app/logs/service.stderr\n"
            "ClassNotFoundException: missing dependency\n"
        ),
    )

    report_path, report = _run_expected_runtime_failure(tmp_path, runner)

    evidence = report["container_evidence"]
    combined = "\n".join(
        f"{entry['state']}\n{entry['logs']}\n{entry['probe']}"
        for entry in evidence.values()
    )
    assert "application refused to start" in combined
    assert "cannot bind 0.0.0.0:6666" in combined
    assert "ClassNotFoundException" in combined
    runtime = "oe-e2e-123456-x86-64-runtime"
    probe_path = report_path.parent / evidence[runtime]["full_probe"]["path"]
    assert "ClassNotFoundException" in probe_path.read_text()
    logs_at = next(
        index
        for index, command in enumerate(runner.calls)
        if command[:2] == ["docker", "logs"]
    )
    cleanup_at = next(
        index
        for index, command in enumerate(runner.calls)
        if command[:3] == ["docker", "rm", "--force"]
    )
    assert logs_at < cleanup_at


def test_runtime_failure_saves_complete_logs_but_reports_bounded_summary(tmp_path):
    container_logs = "".join(f"line-{index:03d}\n" for index in range(250))
    runner = _failing_runtime_runner(container_logs=container_logs)
    report_path, report = _run_expected_runtime_failure(tmp_path, runner)

    evidence = report["container_evidence"]["oe-e2e-123456-x86-64-runtime"]
    metadata = evidence["full_logs"]
    saved = report_path.parent / metadata["path"]
    assert saved.read_text() == container_logs
    assert metadata == {
        "path": metadata["path"],
        "size_bytes": len(container_logs.encode()),
        "capture_status": "complete",
    }
    assert "line-000" not in evidence["logs"]
    assert "line-249" in evidence["logs"]
    log_commands = [
        command for command in runner.calls if command[:2] == ["docker", "logs"]
    ]
    assert log_commands
    assert all("--tail" not in command for command in log_commands)


def test_runtime_failure_marks_timed_out_log_capture_incomplete(tmp_path):
    _, report = _run_expected_runtime_failure(
        tmp_path,
        _failing_runtime_runner(
            container_logs="partial log\n",
            container_logs_returncode=124,
        ),
    )

    metadata = report["container_evidence"][
        "oe-e2e-123456-x86-64-runtime"
    ]["full_logs"]
    assert metadata["capture_status"] == "timeout"
    assert set(metadata) == {"path", "size_bytes", "capture_status"}


def test_evidence_capture_failure_does_not_replace_runtime_report(
    tmp_path,
    monkeypatch,
):
    from scripts.lib import native_validation

    def fail_evidence_write(**_):
        raise OSError("diagnostics unavailable")

    monkeypatch.setattr(
        native_validation,
        "_write_full_evidence",
        fail_evidence_write,
    )
    _, report = _run_expected_runtime_failure(
        tmp_path,
        _failing_runtime_runner(container_logs="application failed\n"),
    )

    assert report["status"] == "failed"
    assert report["failed_stage"] == "wait_healthcheck"
    assert report["container_evidence"] == {
        "capture_error": "diagnostics unavailable"
    }


def test_container_probe_is_generic_and_bounded():
    from scripts.lib.native_validation import _probe_script

    script = _probe_script()
    for root in ("/opt", "/home", "/var/log"):
        assert root in script
    for pattern in ("*.log", "*.stderr"):
        assert pattern in script
    assert "-xdev" in script
    assert "-maxdepth 6" in script
    assert "-mmin -180" in script
    assert "head -n 20" in script
    assert "kvrocks" not in script.lower()


def test_full_evidence_metadata_is_minimal(tmp_path):
    from scripts.lib.native_validation import _write_full_evidence

    metadata = _write_full_evidence(
        artifact_root=tmp_path,
        diagnostics_dir=tmp_path / "diagnostics",
        name="runtime",
        suffix="docker.log",
        content="complete log\n",
    )

    assert metadata == {
        "path": "diagnostics/runtime.docker.log",
        "size_bytes": len(b"complete log\n"),
    }


def test_streamed_command_evidence_writes_directly_to_file(tmp_path):
    from scripts.lib.native_validation import _stream_command_evidence

    path = tmp_path / "diagnostics/runtime.docker.log"
    metadata, summary = _stream_command_evidence(
        command=[
            sys.executable,
            "-c",
            "for index in range(10000): print(f'line-{index:05d}')",
        ],
        cwd=tmp_path,
        artifact_root=tmp_path,
        path=path,
        timeout=30,
    )

    payload = path.read_bytes()
    assert payload.startswith(b"line-00000\n")
    assert payload.endswith(b"line-09999\n")
    assert metadata["size_bytes"] == len(payload)
    assert metadata["capture_status"] == "complete"
    assert "line-00000" not in summary
    assert "line-09999" in summary


def test_release_run_builders_removes_only_current_run_and_architecture(tmp_path):
    from scripts.lib.native_validation import release_run_builders

    runner = RuntimeStateRunner()
    owned = {
        "oe-e2e-123456-x86-64-builder",
        "oe-smoke-123456-x86-64-builder",
    }
    runner.builders.update(
        owned
        | {
            "oe-e2e-999999-x86-64-builder",
            "oe-e2e-123456-aarch64-builder",
        }
    )

    result = release_run_builders(
        run_id="123456",
        architecture="x86_64",
        workspace=tmp_path,
        runner=runner,
    )

    assert set(result["released_builders"]) == owned
    assert owned.isdisjoint(runner.builders)
    assert "oe-e2e-999999-x86-64-builder" in runner.builders
    assert "oe-e2e-123456-aarch64-builder" in runner.builders


@pytest.mark.parametrize("failure", ["list", "remove"])
def test_release_run_builders_propagates_buildx_failures(tmp_path, failure):
    from scripts.lib.native_validation import (
        NativeValidationError,
        release_run_builders,
    )

    class FailingReleaseRunner(RuntimeStateRunner):
        def __call__(self, command, cwd, env, timeout):
            command = list(command)
            if failure == "list" and command[:3] == ["docker", "buildx", "ls"]:
                self.calls.append(command)
                return subprocess.CompletedProcess(
                    command, 1, "", "cannot connect to Docker daemon"
                )
            if failure == "remove" and command[:4] == [
                "docker",
                "buildx",
                "rm",
                "--force",
            ]:
                self.calls.append(command)
                return subprocess.CompletedProcess(
                    command, 1, "", "failed to remove builder"
                )
            return super().__call__(command, cwd, env, timeout)

    runner = FailingReleaseRunner()
    runner.builders.add("oe-e2e-123456-x86-64-builder")

    with pytest.raises(
        NativeValidationError,
        match=(
            "cannot connect to Docker daemon"
            if failure == "list"
            else "failed to remove builder"
        ),
    ):
        release_run_builders(
            run_id="123456",
            architecture="x86_64",
            workspace=tmp_path,
            runner=runner,
        )


def test_release_run_builders_allows_missing_owned_builders(tmp_path):
    from scripts.lib.native_validation import release_run_builders

    result = release_run_builders(
        run_id="123456",
        architecture="x86_64",
        workspace=tmp_path,
        runner=RuntimeStateRunner(),
    )

    assert result["released_builders"] == []


@pytest.mark.parametrize("run_id", ["", "0", "abc", "12x"])
def test_release_run_builders_rejects_invalid_run_id(tmp_path, run_id):
    from scripts.lib.native_validation import (
        NativeValidationError,
        release_run_builders,
    )

    with pytest.raises(NativeValidationError, match="run_id"):
        release_run_builders(
            run_id=run_id,
            architecture="x86_64",
            workspace=tmp_path,
            runner=RuntimeStateRunner(),
        )


@pytest.mark.parametrize("architecture", ["amd64", "arm64", "../x86_64"])
def test_native_validation_rejects_non_runner_architecture_names(
    tmp_path,
    architecture,
):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_image,
    )

    runner = RuntimeStateRunner()
    with pytest.raises(NativeValidationError, match="architecture"):
        validate_native_image(
            workspace=tmp_path,
            task=_task(),
            architecture=architecture,
            run_id="123456",
            report_path=tmp_path / "report.json",
            junit_path=tmp_path / "report.xml",
            runner=runner,
        )

    assert runner.calls == []


class RuntimeSmokeRunner:
    def __init__(self):
        self.calls = []

    @staticmethod
    def _mode(value):
        for mode in (
            "health-ready",
            "terminal",
            "no-probe",
            "probe-timeout",
        ):
            if mode in value:
                return mode
        return ""

    def __call__(self, command, cwd, env, timeout):
        command = list(command)
        self.calls.append(command)
        if command[:3] == ["docker", "buildx", "inspect"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["docker", "image", "inspect"]:
            mode = self._mode(" ".join(command))
            if "--format" in command:
                return subprocess.CompletedProcess(command, 0, f"sha256:{mode}\n", "")
            healthcheck = (
                {"Test": ["CMD", "true"]}
                if mode in {"health-ready", "probe-timeout"}
                else None
            )
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    [
                        {
                            "Config": {
                                "Healthcheck": healthcheck,
                                "ExposedPorts": {},
                            }
                        }
                    ]
                ),
                "",
            )
        if command[0].endswith("wait_ready.sh"):
            mode = self._mode(command[1])
            status = {
                "health-ready": "READY_HEALTH",
                "terminal": "TERMINAL",
                "no-probe": "RUNNING_NO_PROBE",
                "probe-timeout": "PROBE_TIMEOUT",
            }[mode]
            return subprocess.CompletedProcess(
                command, 2 if status == "PROBE_TIMEOUT" else 0, status + "\n", ""
            )
        if command[:3] == ["docker", "inspect", "--format"]:
            mode = self._mode(command[-1])
            state = {
                "Status": "exited" if mode == "terminal" else "running",
                "ExitCode": 7 if mode == "terminal" else 0,
                "OOMKilled": False,
                "Error": "",
            }
            if mode in {"health-ready", "probe-timeout"}:
                state["Health"] = {
                    "Status": "healthy" if mode == "health-ready" else "starting"
                }
            return subprocess.CompletedProcess(command, 0, json.dumps(state), "")
        return subprocess.CompletedProcess(command, 0, "", "")


def test_native_smoke_covers_all_runtime_carriers_without_external_tools(tmp_path):
    from scripts.lib.native_validation import validate_native_smoke

    workspace = _workspace(tmp_path)
    runner = RuntimeSmokeRunner()

    report = validate_native_smoke(
        workspace=workspace,
        task=_task(),
        architecture="x86_64",
        run_id="123456",
        report_path=tmp_path / "reports/smoke.json",
        junit_path=tmp_path / "reports/smoke.junit.xml",
        repair_report_dir=tmp_path / "repairs",
        runner=runner,
    )

    assert report["status"] == "passed"
    assert report["checks"] == {
        "native_build": True,
        "runtime_test": True,
    }
    waiter_calls = [call for call in runner.calls if call[0].endswith("wait_ready.sh")]
    assert {RuntimeSmokeRunner._mode(call[1]) for call in waiter_calls} == {
        "health-ready",
        "terminal",
        "no-probe",
        "probe-timeout",
    }
    assert {
        RuntimeSmokeRunner._mode(call[1]): call[2] for call in waiter_calls
    } == {
        "health-ready": "5",
        "terminal": "5",
        "no-probe": "0",
        "probe-timeout": "0",
    }
    assert len(
        [call for call in runner.calls if "/opt/oe-tests/test.sh" in call]
    ) == 4


def test_native_smoke_does_not_accept_waiter_crash_as_expected_timeout(tmp_path):
    from scripts.lib.native_validation import (
        NativeValidationError,
        validate_native_smoke,
    )

    class CrashedWaiterRunner(RuntimeSmokeRunner):
        def __call__(self, command, cwd, env, timeout):
            if (
                command
                and str(command[0]).endswith("wait_ready.sh")
                and "probe-timeout" in command[1]
            ):
                self.calls.append(list(command))
                return subprocess.CompletedProcess(
                    command, 3, "RUNTIME_ERROR\n", "waiter crashed"
                )
            return super().__call__(command, cwd, env, timeout)

    with pytest.raises(NativeValidationError):
        validate_native_smoke(
            workspace=_workspace(tmp_path),
            task=_task(),
            architecture="x86_64",
            run_id="123456",
            report_path=tmp_path / "reports/smoke.json",
            junit_path=tmp_path / "reports/smoke.junit.xml",
            repair_report_dir=tmp_path / "repairs",
            runner=CrashedWaiterRunner(),
        )
