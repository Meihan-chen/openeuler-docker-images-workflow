import os
import signal
import sys
import time


def test_stream_command_prints_output_and_keeps_it_for_evidence(
    tmp_path,
    capsys,
):
    from scripts.lib.progress import run_streaming

    result = run_streaming(
        [
            sys.executable,
            "-c",
            "print('first line', flush=True); print('second line', flush=True)",
        ],
        cwd=tmp_path,
        env={},
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout == "first line\nsecond line\n"
    assert capsys.readouterr().out == "first line\nsecond line\n"


def test_timeout_kills_the_process_group_after_the_leader_exits(
    tmp_path,
    monkeypatch,
):
    from scripts.lib import progress

    monkeypatch.setattr(progress, "_KILL_GRACE_SECONDS", 0.05)
    program = (
        "import subprocess, time\n"
        "child = subprocess.Popen(\n"
        "    ['/bin/sh', '-c', \"trap '' TERM; exec sleep 30\"],\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "time.sleep(0.1)\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(30)\n"
    )
    result = progress.run_streaming(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env={},
        timeout=0.3,
    )
    child_pid = int(result.stdout.strip())

    def exists() -> bool:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return False
        return True

    try:
        deadline = time.monotonic() + 2
        while exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not exists()
    finally:
        if exists():
            os.kill(child_pid, signal.SIGKILL)

    assert result.returncode == 124
    assert result.abort_reason == "timeout"


def test_watchdog_abort_reason_is_carried_on_the_result(tmp_path, monkeypatch):
    """A watchdog stop must be distinguishable from a budget timeout.

    Run 31065008627 misreported a scratch-cap abort as "TIMEOUT elapsed=480.1s"
    because both paths shared returncode 124; the reason disambiguates them.
    """
    from scripts.lib import progress

    monkeypatch.setattr(progress, "_WATCHDOG_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(progress, "_KILL_GRACE_SECONDS", 0.05)
    result = progress.run_streaming(
        ["/bin/sh", "-c", "sleep 30"],
        cwd=tmp_path,
        env={},
        timeout=10,
        watchdog=lambda: "scratch_over_limit",
    )

    assert result.returncode == 124
    assert result.abort_reason == "scratch_over_limit"
