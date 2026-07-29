import argparse
import json
from dataclasses import dataclass

import pytest


def _task_file(tmp_path):
    path = tmp_path / "task.json"
    path.write_text(
        json.dumps(
            {
                "app": "kvrocks",
                "version": "2.16.0",
                "os_version": "24.03-lts-sp4",
                "domain": "Database",
                "source_url": (
                    "https://github.com/apache/kvrocks/tree/v2.16.0"
                ),
                "scenario": "new-image",
            }
        )
    )
    return path


@dataclass(frozen=True)
class Result:
    status: str = "passed"
    repair_attempts: int = 2
    report: object = None


def _args(tmp_path):
    return argparse.Namespace(
        workspace=tmp_path / "target",
        task_spec=_task_file(tmp_path),
        base_sha="1" * 40,
        architecture="aarch64",
        run_id="123456",
        dgoss=tmp_path / "dgoss",
        goss=tmp_path / "goss",
        report=tmp_path / "reports" / "aarch64.json",
        junit=tmp_path / "reports" / "aarch64.junit.xml",
        repair_report_dir=tmp_path / "reports" / "agents",
        opencode=tmp_path / "opencode",
    )


def test_native_repair_handler_passes_scoped_inputs_and_environment_key(
    tmp_path, monkeypatch, capsys
):
    from scripts.harness import flow

    calls = []
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    monkeypatch.setattr(
        flow,
        "validate_native_with_repairs",
        lambda **kwargs: calls.append(kwargs)
        or Result(report={"status": "passed"}),
    )

    flow.cmd_phase1_native_repair(_args(tmp_path))

    assert len(calls) == 1
    call = calls[0]
    assert call["architecture"] == "aarch64"
    assert call["base_sha"] == "1" * 40
    assert call["api_key"] == "secret"
    assert call["repair_report_dir"].name == "agents"
    assert json.loads(capsys.readouterr().out) == {
        "repair_attempts": 2,
        "report": {"status": "passed"},
        "status": "passed",
    }


def test_native_repair_handler_requires_deepseek_key_before_validation(
    tmp_path, monkeypatch
):
    from scripts.harness import flow
    from scripts.lib.native_repair import NativeRepairError

    calls = []
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        flow,
        "validate_native_with_repairs",
        lambda **kwargs: calls.append(kwargs),
    )

    with pytest.raises(NativeRepairError, match="DEEPSEEK_API_KEY"):
        flow.cmd_phase1_native_repair(_args(tmp_path))

    assert calls == []
