import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap_tools.py"


def _run(*args):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _lock_and_cache(tmp_path):
    data = b"cached-tool"
    digest = hashlib.sha256(data).hexdigest()
    lock = tmp_path / "lock.yml"
    lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cache_root": "/opt/ignored-by-test",
                "tools": {
                    "demo": {
                        "version": "1.2.3",
                        "binary": "demo",
                        "assets": {
                            "x86_64": {
                                "url": "https://invalid.example/demo",
                                "format": "raw",
                                "sha256": digest,
                                "binary_sha256": digest,
                            }
                        },
                    }
                },
            }
        )
    )
    cache = tmp_path / "cache"
    binary = cache / "demo" / "1.2.3" / "x86_64" / "demo"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(data)
    return lock, cache, binary


def test_bootstrap_outputs_resolved_paths_for_workflow(tmp_path):
    lock, cache, binary = _lock_and_cache(tmp_path)
    output_json = tmp_path / "toolchain.json"
    github_output = tmp_path / "github-output"

    result = _run(
        "--lock",
        str(lock),
        "--cache-root",
        str(cache),
        "--arch",
        "x86_64",
        "--output-json",
        str(output_json),
        "--github-output",
        str(github_output),
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(output_json.read_text())
    assert summary["architecture"] == "x86_64"
    assert summary["tools"]["demo"]["path"] == str(binary)
    assert summary["tools"]["demo"]["version"] == "1.2.3"
    assert f"demo_path={binary}" in github_output.read_text()


def test_bootstrap_fails_cleanly_for_unsupported_architecture(tmp_path):
    lock, cache, _ = _lock_and_cache(tmp_path)

    result = _run(
        "--lock",
        str(lock),
        "--cache-root",
        str(cache),
        "--arch",
        "riscv64",
    )

    assert result.returncode == 2
    assert "unsupported Runner architecture" in result.stderr
