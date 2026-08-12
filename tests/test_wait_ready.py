import os
import subprocess
from pathlib import Path

import pytest


WAITER = Path(__file__).parents[1] / "scripts" / "harness" / "wait_ready.sh"


def _fake_docker(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/bin/bash
set -eu
scenario=${FAKE_DOCKER_SCENARIO:?}
if [[ $1 == inspect ]]; then
    case "$scenario" in
        healthy) printf 'running\0370\037false\037\037healthy\n' ;;
        terminal) printf 'exited\03717\037false\037\037\n' ;;
        running) printf 'running\0370\037false\037\037\n' ;;
        starting) printf 'running\0370\037false\037\037starting\n' ;;
        oom) printf 'exited\037137\037true\037\037\n' ;;
        state_error) printf 'exited\0371\037false\037shim failed\037\n' ;;
        tcp) printf 'running\0370\037false\037\037\n' ;;
        tcp_exit_race)
            if [[ ! -e $FAKE_DOCKER_STATE ]]; then
                : >"$FAKE_DOCKER_STATE"
                printf 'running\0370\037false\037\037\n'
            else
                printf 'exited\0379\037false\037\037\n'
            fi
            ;;
        inspect_error) echo 'daemon unavailable' >&2; exit 1 ;;
    esac
elif [[ $1 == exec ]]; then
    [[ $scenario != tcp_exit_race ]] || { echo 'container is not running' >&2; exit 1; }
    port=${!#}
    [[ $scenario == tcp && $port == 8443 ]] && printf 'OPEN\n' || printf 'CLOSED\n'
else
    echo "unexpected docker command: $*" >&2
    exit 2
fi
"""
    )
    docker.chmod(0o755)
    return bin_dir


def _run_waiter(tmp_path, scenario, *arguments):
    assert WAITER.is_file(), "production waiter is missing"
    env = os.environ.copy()
    env["PATH"] = f"{_fake_docker(tmp_path)}:{env['PATH']}"
    env["FAKE_DOCKER_SCENARIO"] = scenario
    env["FAKE_DOCKER_STATE"] = str(tmp_path / "docker-state")
    return subprocess.run(
        [str(WAITER), "candidate", *map(str, arguments)],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


@pytest.mark.parametrize(
    ("scenario", "mode", "expected"),
    [
        ("healthy", "health", "READY_HEALTH"),
        ("terminal", "none", "TERMINAL"),
        ("running", "none", "RUNNING_NO_PROBE"),
        ("starting", "health", "PROBE_TIMEOUT"),
    ],
)
def test_waiter_reports_event_race_outcome(tmp_path, scenario, mode, expected):
    result = _run_waiter(tmp_path, scenario, 0, mode)

    assert result.stdout.splitlines()[0] == expected
    assert result.returncode == (2 if expected == "PROBE_TIMEOUT" else 0)


def test_waiter_reports_health_readiness_elapsed_time(tmp_path):
    result = _run_waiter(tmp_path, "healthy", 0, "health")

    assert result.returncode == 0
    elapsed = next(
        line.partition("=")[2]
        for line in result.stderr.splitlines()
        if line.startswith("time_to_healthy_seconds=")
    )
    assert elapsed.isdigit()


def test_waiter_rejects_tcp_readiness_mode(tmp_path):
    result = _run_waiter(tmp_path, "tcp", 0, "tcp", 8080, 8443)

    assert result.returncode == 3
    assert result.stdout.splitlines()[0] == "RUNTIME_ERROR"
    assert "health|none" in result.stderr


def test_waiter_rejects_probe_arguments_in_no_probe_mode(tmp_path):
    result = _run_waiter(tmp_path, "running", 0, "none", 8080)

    assert result.returncode == 3
    assert result.stdout.splitlines()[0] == "RUNTIME_ERROR"


@pytest.mark.parametrize("scenario", ["oom", "state_error", "inspect_error"])
def test_waiter_reports_runtime_errors(tmp_path, scenario):
    result = _run_waiter(tmp_path, scenario, 0, "none")

    assert result.returncode == 3
    assert result.stdout.splitlines()[0] == "RUNTIME_ERROR"
