"""Deterministic routing of a failure to the action that can resolve it."""

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
    assert "no agent may" in result["guidance"].lower()
    assert "stop" in result["guidance"].lower()


def test_a_real_candidate_scope_violation_is_not_workspace_hygiene():
    from scripts.lib.failure_classification import classify_failure

    gate = {
        "status": "failed",
        "errors": _scope_errors(["Database/redis/meta.yml"]),
    }

    result = classify_failure(gate=gate, allowed_roots=("Database",))

    assert result["category"] == "candidate-scope"


@pytest.mark.parametrize(
    ("stage", "category"),
    (
        ("native_build", "build-error"),
        ("runtime_test", "runtime-error"),
        ("test_sh", "runtime-error"),
        ("post_inspect", "runtime-error"),
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


def test_invalid_generated_tests_are_returned_to_testcase_creator():
    from scripts.lib.failure_classification import classify_failure

    result = classify_failure(
        report={
            "status": "failed",
            "failed_stage": "test_contract",
            "failure": "native test contract is not executable",
        }
    )

    assert result["category"] == "test-contract"


def test_a_build_timeout_is_infrastructure_and_names_no_candidate_repair():
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


def test_a_target_clone_disconnect_is_infrastructure_not_candidate_failure():
    from scripts.lib.failure_classification import classify_failure

    result = classify_failure(
        report={
            "status": "failed",
            "failed_stage": "target_clone",
            "failure": "fatal: early EOF",
            "failure_details": {"attempts": 2, "retryable": True},
        }
    )

    assert result["category"] == "infra"
    assert "do not modify" in result["guidance"].lower()


@pytest.mark.parametrize(
    ("kind", "category", "owner"),
    (
        ("candidate", "image-contract", "image_creator"),
        ("infra", "infra", None),
    ),
)
def test_upstream_format_failure_routes_by_evidence_kind(kind, category, owner):
    from scripts.lib.failure_classification import classify_failure

    result = classify_failure(
        report={
            "status": "failed",
            "failed_stage": "upstream_format",
            "failure": "upstream format check failed",
            "failure_details": {"kind": kind},
            "format_check": {
                "status": "failed",
                "kind": kind,
                "output": "image-info.yml is missing environment",
            },
        }
    )

    assert result["category"] == category
    if owner is None:
        assert "owner" not in result
    else:
        assert result["owner"] == owner


@pytest.mark.parametrize("stage", ("runtime_test", "test_sh"))
def test_a_candidate_runtime_timeout_is_not_misclassified_as_infra(stage):
    from scripts.lib.failure_classification import classify_failure

    result = classify_failure(
        report={
            "status": "failed",
            "failed_stage": stage,
            "failure": "command timed out",
            "failure_details": {"returncode": 124},
        }
    )

    assert result["category"] == "runtime-error"


def test_a_hard_stop_gate_is_classified_as_non_agent_repair():
    from scripts.lib.failure_classification import classify_failure

    result = classify_failure(
        gate={
            "status": "failed",
            "build_allowed": False,
            "findings": [
                {
                    "code": "scope.changed_path",
                    "level": "hard_stop",
                    "owner": "workflow",
                    "message": "changed path is outside the task scope",
                }
            ],
        }
    )

    assert result["category"] == "hard-stop"
    assert result["finding_codes"] == ["scope.changed_path"]


def test_missing_evidence_is_reported_rather_than_guessed():
    from scripts.lib.failure_classification import classify_failure

    assert classify_failure()["category"] == "unclassified"


@pytest.mark.parametrize(
    "stage",
    (
        "runtime_test",
        "default_start",
        "wait_healthcheck",
        "wait_tcp",
        "test_sh",
        "post_inspect",
    ),
)
def test_runtime_test_substages_route_to_runtime_repair(stage):
    from scripts.lib.failure_classification import classify_failure

    result = classify_failure(
        report={
            "status": "failed",
            "failed_stage": stage,
            "failure": "runtime validation failed",
            "failure_details": {"returncode": 1},
        }
    )

    assert result["category"] == "runtime-error"


def test_a_modified_tracked_file_is_scope_not_research_junk():
    """`M README.md` must never produce "delete or move README.md".

    Only an added, previously unknown path can be research output; a modified
    or deleted tracked file is the candidate reaching outside its own scope.
    """
    from scripts.lib.failure_classification import classify_failure

    gate = {
        "status": "failed",
        "errors": ["change outside task scope or wrong status: M README.md"],
    }

    result = classify_failure(gate=gate, allowed_roots=("Database",))

    assert result["category"] == "candidate-scope"
    assert "stray_paths" not in result


def test_added_junk_beside_a_modified_file_is_still_scope():
    """One tracked-file violation is enough to rule out a pure cleanup."""
    from scripts.lib.failure_classification import classify_failure

    gate = {
        "status": "failed",
        "errors": [
            "change outside task scope or wrong status: A kvrocks-2.16.0/CMakeLists.txt",
            "change outside task scope or wrong status: M README.md",
        ],
    }

    assert (
        classify_failure(gate=gate, allowed_roots=("Database",))["category"]
        == "candidate-scope"
    )


def test_stray_added_files_at_the_repo_root_are_hygiene():
    """Run 30597380057 left .baseimg.tar.xz and .filelists.tmp behind."""
    from scripts.lib.failure_classification import classify_failure

    gate = {
        "status": "failed",
        "errors": [
            "change outside task scope or wrong status: A .baseimg.tar.xz",
            "change outside task scope or wrong status: A .filelists.tmp",
        ],
    }

    result = classify_failure(gate=gate, allowed_roots=("Database",))

    assert result["category"] == "workspace-hygiene"
    assert result["stray_paths"] == [".baseimg.tar.xz", ".filelists.tmp"]


def test_yaml_parse_text_during_a_build_is_a_build_error():
    """Upstream YAML processed inside a RUN step remains a build failure."""
    from scripts.lib.failure_classification import classify_failure

    report = {
        "status": "failed",
        "failed_stage": "native_build",
        "failure": "yaml: line 12: mapping values are not allowed",
        "failure_details": {"returncode": 1},
    }

    assert classify_failure(report=report)["category"] == "build-error"


def test_a_lint_failure_names_the_linter_rather_than_asking_for_more_evidence():
    """Hadolint is diagnostic and must not invent an Agent repair."""
    from scripts.lib.failure_classification import classify_failure

    result = classify_failure(
        gate={"status": "passed"},
        lint={
            "status": "passed",
            "diagnostic_status": "findings",
            "blocking": False,
            "output": "DL3033 pin yum packages",
        },
    )

    assert result["category"] == "lint-advisory"
    assert "do not request" in result["guidance"].lower()


@pytest.mark.parametrize(
    ("owner", "category"),
    (
        ("image_creator", "image-contract"),
        ("testcase_creator", "test-contract"),
    ),
)
def test_structured_gate_finding_routes_to_its_declared_owner(owner, category):
    from scripts.lib.failure_classification import classify_failure

    gate = {
        "status": "passed",
        "build_allowed": True,
        "delivery_allowed": False,
        "findings": [
            {
                "code": "contract.example",
                "level": "delivery_stop",
                "owner": owner,
                "message": "repair this declared contract",
            }
        ],
    }

    result = classify_failure(gate=gate)

    assert result["category"] == category
    assert result["owner"] == owner
    assert result["finding_codes"] == ["contract.example"]
