"""End-to-end integration test — simulates full flow with mock opencode."""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, "scripts/harness")
sys.path.insert(0, "scripts/utils")

# opencode is called via subprocess, mock out _run_opencode
os.environ["SKIP_OPENCODE_CHECK"] = "1"


class TestNewImageFlow:
    """Simulates the new-image workflow end-to-end."""

    def test_parse_issue(self):
        from parse_issue import parse_issue_body, validate

        body = Path("tests/fixtures/sample-issue.md").read_text()
        fields = parse_issue_body(body)

        assert fields["package_name"] == "nginx"
        assert fields["domain"] == "Cloud"
        assert fields["os_version"] == "24.03-lts"
        assert fields["os_tag"] == "oe2403lts"
        assert validate(fields) == []

    def test_gate_diff_only_additions(self):
        from gate_diff import is_allowed_modification, get_changed_files

        # meta.yml and README.md can be appended
        assert is_allowed_modification("AI/test-app/meta.yml") is True
        assert is_allowed_modification("AI/test-app/README.md") is True
        assert is_allowed_modification("AI/image-list.yml") is True
        # Dockerfile and test files cannot be modified
        assert is_allowed_modification("AI/test-app/1.0/24.03-lts/Dockerfile") is False
        assert is_allowed_modification("AI/test-app/tests/goss.yaml") is False

    def test_meta_validation_flow(self):
        from validate_meta import validate_meta_file

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create valid directory structure
            dockerfile_dir = os.path.join(tmpdir, "1.0", "24.03-lts")
            os.makedirs(dockerfile_dir)
            Path(os.path.join(dockerfile_dir, "Dockerfile")).touch()

            meta_content = """1.0-oe2403lts:
  path: 1.0/24.03-lts/Dockerfile
"""
            meta_path = os.path.join(tmpdir, "meta.yml")
            with open(meta_path, "w") as f:
                f.write(meta_content)

            errors = validate_meta_file(meta_path)
            assert errors == []

    def test_scoring_pipeline(self):
        from scoring import calculate_confidence

        result = calculate_confidence(
            build_success={"x86_64": True, "aarch64": True},
            test_pass_rate={"x86_64": 1.0, "aarch64": 1.0},
            hadolint_violations=0,
            meta_consistent=True,
        )
        assert result["score"] == 1.0
        assert result["level"] == "auto-merge"

    def test_scoring_with_failures(self):
        from scoring import calculate_confidence

        result = calculate_confidence(
            build_success={"x86_64": False, "aarch64": True},
            test_pass_rate={"x86_64": 0.0, "aarch64": 0.5},
            hadolint_violations=3,
            meta_consistent=False,
        )
        assert result["score"] < 0.75
        assert result["level"] == "loop"


class TestVersionUpdateFlow:
    """Simulates the version-update workflow."""

    @patch("query_version._http_get")
    def test_query_version_with_anitya_response(self, mock_get):
        from query_version import query_app_version

        mock_get.return_value = {
            "items": [
                {"tag": "app_up", "version": "1.28.0"},
                {"tag": "app_openeuler", "version": "1.27.2", "raw_versions": ["1.27.2-24.03-lts"]},
            ]
        }

        result = query_app_version("nginx")
        assert result["upstream_version"] == "1.28.0"
        assert result["oe_version"] == "1.27.2"
        assert result["os_version"] == "24.03-lts"

    @patch("query_version._http_get")
    def test_version_unchanged_no_update(self, mock_get):
        from query_version import query_app_version

        mock_get.return_value = {
            "items": [
                {"tag": "app_up", "version": "1.27.2"},
                {"tag": "app_openeuler", "version": "1.27.2", "raw_versions": ["1.27.2-24.03-lts"]},
            ]
        }

        result = query_app_version("nginx")
        assert result["upstream_version"] == result["oe_version"]  # no update needed


class TestPRComposition:
    """Simulates PR composition."""

    def test_pr_title_generation(self):
        os.environ["PACKAGE"] = "nginx"
        os.environ["APP_VERSION"] = "1.28.0"
        os.environ["OS_VERSION"] = "24.03-lts"

        from compose_pr import compose_pr_title
        title = compose_pr_title("new-image")
        assert "nginx" in title
        assert "1.28.0" in title
        assert "24.03-lts" in title
        assert title.startswith("[new-image]")

    def test_pr_body_structure(self):
        os.environ["PACKAGE"] = "nginx"
        os.environ["APP_VERSION"] = "1.28.0"
        os.environ["OS_VERSION"] = "24.03-lts"

        from compose_pr import compose_pr_body
        body = compose_pr_body("new-image")
        assert "## Automated PR" in body
        assert "### Changes" in body
        assert "### Build Proof" in body
        assert "### Test Results" in body