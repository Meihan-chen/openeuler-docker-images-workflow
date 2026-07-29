"""Small, unbuffered progress helpers shared by workflow stages."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Callable, Mapping, Sequence


def log(stage: str, message: str) -> None:
    print(f"[flow][{stage}] {message}", flush=True)


def run_streaming(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
    emit: Callable[[str], None] | None = None,
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
    )
    timed_out = [False]

    def expire() -> None:
        timed_out[0] = True
        process.kill()

    timer = threading.Timer(timeout, expire)
    timer.start()
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
    if timed_out[0]:
        returncode = 124
    return subprocess.CompletedProcess(
        args=list(command),
        returncode=returncode,
        stdout="".join(output),
        stderr="",
    )
