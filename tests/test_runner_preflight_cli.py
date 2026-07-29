import json
from pathlib import Path


GIB = 1024**3


def test_collect_snapshot_reads_linux_runner_capabilities(tmp_path):
    from scripts.runner_preflight import collect_snapshot

    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable: 14680064 kB\n")
    toolchain = tmp_path / "toolchain.json"
    toolchain.write_text(
        json.dumps(
            {
                "tools": {
                    "goss": {
                        "path": "/opt/oe-image-tools/goss/0.4.10/x86_64/goss"
                    }
                }
            }
        )
    )
    commands = {
        ("docker", "version", "--format", "{{.Server.Version}}"): "27.5.1",
        ("docker", "buildx", "version"): "github.com/docker/buildx v0.20.0",
    }

    snapshot = collect_snapshot(
        workspace=tmp_path,
        toolchain_json=toolchain,
        meminfo_path=meminfo,
        machine="amd64",
        cpu_count=8,
        command_output=lambda command: commands[tuple(command)],
        disk_free=lambda path: 32 * GIB,
    )

    assert snapshot.architecture == "amd64"
    assert snapshot.cpu_count == 8
    assert snapshot.memory_available_bytes == 14680064 * 1024
    assert snapshot.disk_free_bytes == 32 * GIB
    assert snapshot.docker_server_version == "27.5.1"
    assert snapshot.tools["goss"] == Path(
        "/opt/oe-image-tools/goss/0.4.10/x86_64/goss"
    )


def test_preflight_cli_writes_passed_report(tmp_path, monkeypatch):
    from scripts import runner_preflight
    from scripts.lib.toolchain import RunnerSnapshot

    tools = {}
    for name in ("dgoss", "goss", "hadolint", "jq", "opencode"):
        path = tmp_path / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        tools[name] = path
    snapshot = RunnerSnapshot(
        architecture="x86_64",
        cpu_count=8,
        memory_available_bytes=14 * GIB,
        disk_free_bytes=32 * GIB,
        docker_server_version="27.5.1",
        buildx_version="github.com/docker/buildx v0.20.0",
        tools=tools,
    )
    monkeypatch.setattr(
        runner_preflight,
        "collect_snapshot",
        lambda **kwargs: snapshot,
    )
    output = tmp_path / "preflight.json"

    result = runner_preflight.main(
        [
            "--expected-arch",
            "x86_64",
            "--workspace",
            str(tmp_path),
            "--toolchain-json",
            str(tmp_path / "tools.json"),
            "--output-json",
            str(output),
        ]
    )

    assert result == 0
    assert json.loads(output.read_text())["status"] == "passed"


def test_missing_system_command_returns_empty_capability():
    from scripts.runner_preflight import command_output

    assert command_output(["command-that-does-not-exist-oe-preflight"]) == ""
