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
  - id: goss_schema_mismatch
    title: Goss rejects an unknown resource attribute
    stages: [dgoss]
    symptom_keywords: ["invalid Attribute for", "dgoss"]
    diagnosis: The gossfile does not match the pinned Goss schema.
    remediation:
      - Use an attribute supported by the pinned Goss resource schema.
    prevention: Validate the complete gossfile during native dgoss execution.
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
        "goss_schema_mismatch",
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
        "failed_stage": "dgoss",
        "failure": "Error: invalid Attribute for File:/data: dir",
    }

    selected = select_patterns(patterns, evidence)

    assert [pattern.id for pattern in selected] == ["goss_schema_mismatch"]


def test_renders_index_and_only_matched_sections():
    from scripts.lib.failure_knowledge import render_knowledge

    rendered = render_knowledge(
        _DOCUMENT,
        {"failure": "invalid Attribute for File:/data: dir"},
    )

    assert "goss_schema_mismatch" in rendered
    assert "The gossfile does not match the pinned Goss schema" in rendered
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
        "goss_schema_mismatch",
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
        "goss_schema_mismatch": {
            "failed_stage": "dgoss",
            "failure": "invalid Attribute for File:/data: dir",
        },
        "protocol_command_semantics_mismatch": {
            "failed_stage": "shared_tests",
            "failure": "functional roundtrip passed but metadata assertion failed",
        },
    }
    assert {
        pattern_id: select_patterns(patterns, evidence)[0].id
        for pattern_id, evidence in evidence_by_pattern.items()
    } == {pattern_id: pattern_id for pattern_id in evidence_by_pattern}
