"""Tests for parse_issue.py."""

import sys
sys.path.insert(0, "scripts/harness")

from parse_issue import parse_issue_body, validate


SAMPLE_ISSUE = """**Package Name**: nginx
**Upstream Repository**: https://github.com/nginx/nginx
**Domain**: Cloud
**openEuler Version**: 24.03-lts
**App Version**: 1.27.2

## Description
Nginx is a high-performance HTTP server and reverse proxy.
"""


def test_parse_all_fields():
    fields = parse_issue_body(SAMPLE_ISSUE)
    assert fields["package_name"] == "nginx"
    assert fields["source_repo_url"] == "https://github.com/nginx/nginx"
    assert fields["domain"] == "Cloud"
    assert fields["os_version"] == "24.03-lts"
    assert fields["app_version"] == "1.27.2"


def test_validate_complete():
    fields = parse_issue_body(SAMPLE_ISSUE)
    missing = validate(fields)
    assert missing == []


def test_validate_missing():
    missing = validate({"package_name": "nginx"})
    assert "source_repo_url" in missing
    assert "domain" in missing


def test_derived_os_tag():
    fields = parse_issue_body(SAMPLE_ISSUE)
    assert fields["os_tag"] == "oe2403lts"


def test_domain_normalization():
    body = SAMPLE_ISSUE.replace("Cloud", "database")
    fields = parse_issue_body(body)
    assert fields["domain"] == "Database"