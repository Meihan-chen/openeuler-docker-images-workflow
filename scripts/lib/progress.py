"""Small, unbuffered progress helpers shared by workflow stages."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable, Mapping, Sequence

# Give a timed-out process a short cleanup window, then stop its whole group.
_KILL_GRACE_SECONDS = 15.0
_WATCHDOG_INTERVAL_SECONDS = 60.0


def log(stage: str, message: str) -> None:
    print(f"[flow][{stage}] {message}", flush=True)


def run_streaming(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
    emit: Callable[[str], None] | None = None,
    watchdog: Callable[[], str | None] | None = None,
) -> subprocess.CompletedProcess:
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
        # Own process group so a stopped stage cannot leave downloads or
        # container builds running as orphans.
        start_new_session=True,
    )
    process_group = process.pid
    timed_out = [False]
    escalation: list[threading.Timer | None] = [None]
    finished = threading.Event()
    stopping = threading.Event()

    def signal_group(number: int) -> None:
        try:
            os.killpg(process_group, number)
        except ProcessLookupError:
            return
        except OSError:
            if process.poll() is None:
                process.send_signal(number)

    def stop() -> None:
        # The budget timer and the watchdog can fire together; escalating twice
        # would leave a live timer able to signal a recycled process id.
        if stopping.is_set():
            return
        stopping.set()
        timed_out[0] = True
        signal_group(signal.SIGTERM)
        escalation[0] = threading.Timer(
            _KILL_GRACE_SECONDS,
            lambda: signal_group(signal.SIGKILL),
        )
        escalation[0].start()

    def watch() -> None:
        while not finished.wait(_WATCHDOG_INTERVAL_SECONDS):
            if watchdog is not None and watchdog() is not None:
                stop()
                return

    timer = threading.Timer(timeout, stop)
    timer.start()
    watch_thread: threading.Thread | None = None
    if watchdog is not None:
        watch_thread = threading.Thread(
            target=watch,
            name="run-streaming-watchdog",
            daemon=True,
        )
        watch_thread.start()
    output: list[str] = []
    try:
        assert process.stdout is not None
        for line in process.stdout:
            output.append(line)
            if emit is None:
                print(line, end="", flush=True)
            else:
                emit(line)
        returncode = process.wait()
    finally:
        timer.cancel()
        finished.set()
        if watch_thread is not None:
            watch_thread.join(timeout=1)
        # The group leader may exit before a child that ignored SIGTERM. Kill
        # any remainder while the captured process-group id is still ours.
        if escalation[0] is not None:
            signal_group(signal.SIGKILL)
            escalation[0].cancel()
    if timed_out[0]:
        returncode = 124
    return subprocess.CompletedProcess(
        args=list(command),
        returncode=returncode,
        stdout="".join(output),
        stderr="",
    )
