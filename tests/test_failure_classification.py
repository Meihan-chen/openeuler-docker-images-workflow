"""Deterministic routing of a failure to the action that can resolve it.

The Fixer used to receive free-text evidence that pointed at the wrong repair:
Run 30480464176 read a Goss config parse error as a UID permission problem, and
Run 30567356119 expressed one unpacked tarball as 496 out-of-scope file errors.
"""

import pytest


def _scope_errors(paths):
    return [
        f"change outside task scope or wrong status: A {path}" for path in paths
    ]


def test_one_unpacked_tarball_is_workspace_hygiene_not_a_candidate_revert():
    """Reproduces Run 30567356119: 496 errors, all from `curl | tar -xz`."""
    from scripts.lib.failure_classification import classify_failure

    gate = {
        "status": "failed",
        "errors": _scope_errors(
            [
                "kvrocks-2.16.0/.asf.yaml",
                "kvrocks-2.16.0/CMakeLists.txt",
                "kvrocks-2.16.0/src/cli/main.cc",
                "kvrocks-2.16.0/utils/systemd/kvrocks.service",
            ]
        ),
    }

    result = classify_failure(gate=gate, allowed_roots=("Database",))

    assert result["category"] == "workspace-hygiene"
    assert result["stray_paths"] == ["kvrocks-2.16.0"]
    assert "kvrocks-2.16.0" in result["guidance"]
    assert "do not revert" in result["guidance"].lower()


def test_a_real_candidate_scope_violation_is_not_workspace_hygiene():
    from scripts.lib.failure_classification import classify_failure

    gate = {
        "status": "failed",
        "errors": _scope_errors(["Database/redis/meta.yml"]),
    }

    result = classify_failure(gate=gate, allowed_roots=("Database",))

    assert result["category"] == "candidate-scope"


def test_goss_parse_failure_is_config_parse_not_a_runtime_problem():
    """Reproduces Run 30480464176 and the `dir: true` failure in 30599107031."""
    from scripts.lib.failure_classification import classify_failure

    report = {
        "status": "failed",
        "failed_stage": "dgoss",
        "failure": "invalid Attribute for File:/var/lib/kvrocks: dir",
        "failure_details": {"returncode": 1},
    }

    result = classify_failure(report=report)

    assert result["category"] == "config-parse"
    assert "goss" in result["guidance"].lower()


@pytest.mark.parametrize(
    ("stage", "category"),
    (
        ("native_build", "build-error"),
        ("dgoss", "runtime-error"),
        ("shared_tests", "runtime-error"),
        ("restart_persistence", "runtime-error"),
    ),
)
def test_failed_stage_separates_build_from_runtime(stage, category):
    from scripts.lib.failure_classification import classify_failure

    report = {
        "status": "failed",
        "failed_stage": stage,
        "failure": "command failed",
        "failure_details": {"returncode": 1},
    }

    assert classify_failure(report=report)["category"] == category


def test_a_timeout_is_infrastructure_and_names_no_candidate_repair():
    from scripts.lib.failure_classification import classify_failure

    report = {
        "status": "failed",
        "failed_stage": "native_build",
        "failure": "timed out",
        "failure_details": {"returncode": 124},
    }

    result = classify_failure(report=report)

    assert result["category"] == "infra"
    assert "do not" in result["guidance"].lower()


def test_missing_evidence_is_reported_rather_than_guessed():
    from scripts.lib.failure_classification import classify_failure

    assert classify_failure()["category"] == "unclassified"
