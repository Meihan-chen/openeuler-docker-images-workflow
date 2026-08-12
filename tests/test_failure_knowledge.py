from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "docs" / "failure-patterns.yml"

_DOCUMENT = """\
schema_version: 1
patterns:
  - id: artifact_source_unavailable
    title: Pinned artifact source is unavailable
    stages: [native_build]
    symptom_keywords: ["404 Not Found", "download"]
    diagnosis: The pinned source does not serve the requested artifact.
    remediation:
      - Confirm the requested version and select a version-pinned authoritative source.
    prevention: Resolve and hash the artifact before native builds.
    verification:
      status: verified
      sources: ["regression:tests/test_failure_knowledge.py"]
  - id: environment_variable_self_reference
    title: Environment variable self-reference is not portable
    stages: [generated_contract]
    symptom_keywords: ["UndefinedVar", "ENV", "LD_LIBRARY_PATH"]
    diagnosis: A Dockerfile references a variable before defining it.
    remediation:
      - Define a complete value without assuming inherited state.
    prevention: Keep Dockerfile linting in the deterministic generation gate.
    verification:
      status: verified
      sources: ["regression:tests/test_failure_knowledge.py"]
  - id: protocol_command_semantics_mismatch
    title: A protocol command has application-specific semantics
    stages: [runtime_test]
    symptom_keywords: ["runtime_test", "metadata assertion failed"]
    diagnosis: The test assumes unsupported command semantics.
    remediation:
      - Use a command assertion supported by fixed upstream evidence.
    prevention: Review application command semantics before native execution.
    verification:
      status: verified
      sources: ["regression:tests/test_native_validation.py"]
"""


def test_parses_verified_structured_patterns():
    from scripts.lib.failure_knowledge import parse_patterns

    patterns = parse_patterns(_DOCUMENT)

    assert [pattern.id for pattern in patterns] == [
        "artifact_source_unavailable",
        "environment_variable_self_reference",
        "protocol_command_semantics_mismatch",
    ]
    assert patterns[0].keywords == ("404 Not Found", "download")
    assert patterns[2].verification_sources == (
        "regression:tests/test_native_validation.py",
    )


def test_rejects_unverified_or_duplicate_patterns():
    from scripts.lib.failure_knowledge import FailureKnowledgeError, parse_patterns

    unverified = _DOCUMENT.replace("status: verified", "status: draft", 1)
    duplicate = _DOCUMENT + _DOCUMENT.split("patterns:\n", 1)[1].split(
        "  - id: environment_variable_self_reference", 1
    )[0]

    with pytest.raises(FailureKnowledgeError, match="verified"):
        parse_patterns(unverified)
    with pytest.raises(FailureKnowledgeError, match="duplicate"):
        parse_patterns(duplicate)


def test_bare_identifiers_match_on_word_boundaries():
    from scripts.lib.failure_knowledge import parse_patterns, select_patterns

    patterns = parse_patterns(_DOCUMENT)
    evidence = {"failure": "the environment reported 51404 bytes"}

    assert select_patterns(patterns, evidence) == ()


def test_ranks_the_pattern_whose_symptoms_match():
    from scripts.lib.failure_knowledge import parse_patterns, select_patterns

    patterns = parse_patterns(_DOCUMENT)
    evidence = {
        "failed_stage": "runtime_test",
        "failure": "functional roundtrip passed but metadata assertion failed",
    }

    selected = select_patterns(patterns, evidence)

    assert [pattern.id for pattern in selected] == [
        "protocol_command_semantics_mismatch"
    ]


def test_renders_index_and_only_matched_sections():
    from scripts.lib.failure_knowledge import render_knowledge

    rendered = render_knowledge(
        _DOCUMENT,
        {"failure": "runtime_test metadata assertion failed"},
    )

    assert "protocol_command_semantics_mismatch" in rendered
    assert "unsupported command semantics" in rendered
    assert "The pinned source does not serve" not in rendered
    assert "regression:tests/test_native_validation.py" in rendered


def test_renders_a_miss_without_expanding_sections():
    from scripts.lib.failure_knowledge import render_knowledge

    rendered = render_knowledge(_DOCUMENT, {"failure": "no known symptom"})

    assert "No verified pattern matched this failure" in rendered
    assert "The pinned source does not serve" not in rendered


def test_shipped_knowledge_is_small_verified_and_application_neutral():
    from scripts.lib.failure_knowledge import parse_patterns, select_patterns

    document = KNOWLEDGE.read_text()
    patterns = parse_patterns(document)

    assert {pattern.id for pattern in patterns} == {
        "runtime_identity_collision",
        "build_feature_dependency_mismatch",
        "protocol_command_semantics_mismatch",
    }
    assert all(pattern.verification_sources for pattern in patterns)
    regression_sources = [
        source.removeprefix("regression:")
        for pattern in patterns
        for source in pattern.verification_sources
        if source.startswith("regression:")
    ]
    assert all((ROOT / source).is_file() for source in regression_sources)
    assert "kvrocks" not in document.lower()
    assert "DBSIZE" not in document
    assert "模式01" not in document
    assert "模式36" not in document
    assert "→" not in document

    evidence_by_pattern = {
        "runtime_identity_collision": {
            "failure": "groupadd: GID 999 is not unique; exit code: 4"
        },
        "build_feature_dependency_mismatch": {
            "failure": "cannot find static library; disable ENABLE_STATIC=OFF"
        },
        "protocol_command_semantics_mismatch": {
            "failed_stage": "runtime_test",
            "failure": "functional roundtrip passed but metadata assertion failed",
        },
    }
    assert {
        pattern_id: select_patterns(patterns, evidence)[0].id
        for pattern_id, evidence in evidence_by_pattern.items()
    } == {pattern_id: pattern_id for pattern_id in evidence_by_pattern}


@pytest.mark.parametrize(
    ("failed_stage", "check", "failure", "native_build", "runtime_test"),
    (
        ("native_build", "native_build", "compiler exited 2", False, None),
        ("wait_healthcheck", "runtime_test", "PROBE_TIMEOUT", True, False),
        (
            "test_sh",
            "runtime_test",
            "syntax error near unexpected token",
            True,
            False,
        ),
        (
            "test_sh",
            "runtime_test",
            "functional roundtrip passed",
            True,
            False,
        ),
        (
            "test_sh",
            "runtime_test",
            "TESTS_FAILED: 1 failure(s)",
            True,
            False,
        ),
        (
            "post_inspect",
            "runtime_test",
            "container was OOMKilled",
            True,
            False,
        ),
    ),
)
def test_shipped_protocol_pattern_ignores_generic_native_failure_structure(
    failed_stage,
    check,
    failure,
    native_build,
    runtime_test,
):
    from scripts.lib.failure_knowledge import parse_patterns, select_patterns

    patterns = parse_patterns(KNOWLEDGE.read_text())
    evidence = {
        "kind": "native_validation_failure",
        "architectures": {
            "x86_64": {
                "status": "failed",
                "checks": {
                    "native_build": native_build,
                    "runtime_test": runtime_test,
                },
                "failed_stage": failed_stage,
                "failure": failure,
                "failures": [
                    {
                        "stage": failed_stage,
                        "check": check,
                        "failure": failure,
                        "failure_details": {},
                    }
                ],
            }
        },
    }

    selected_ids = {pattern.id for pattern in select_patterns(patterns, evidence)}

    assert "protocol_command_semantics_mismatch" not in selected_ids


def test_shipped_knowledge_uses_the_native_runtime_stage_only():
    from scripts.lib.failure_knowledge import parse_patterns

    document = KNOWLEDGE.read_text()
    patterns = parse_patterns(document)

    assert any(
        "runtime_test" in pattern.stages
        for pattern in patterns
        if pattern.id == "protocol_command_semantics_mismatch"
    )
