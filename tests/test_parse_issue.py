"""Tests for parse_issue.py."""

import sys
sys.path.insert(0, "scripts/harness")

import pytest

from parse_issue import (
    IssueParseError,
    parse_issue_body,
    parse_issue_request,
    validate,
)


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


def test_target_repository_virtualization_domain_maps_to_cloud():
    body = (
        "**软件包名称（Package Name）：** kubeflow\n"
        "**源码仓库（Source Repository）：** "
        "https://github.com/kubeflow/kubeflow\n"
        "**所属领域（Domain）：** 虚拟化\n"
    )

    assert parse_issue_body(body)["domain"] == "Cloud"


MINIMAL_TARGET_ISSUE = """**软件包名称（Package Name）：** kvrocks
**源码仓库（Source Repository）：** https://github.com/apache/kvrocks/tree/v2.16.0
**所属领域（Domain）：** 数据库
"""


def test_parse_target_repository_minimal_bilingual_issue():
    fields = parse_issue_request(
        "【new-image】add kvrocks 2.16.0 docker image on openEuler 24.03-LTS-SP4",
        MINIMAL_TARGET_ISSUE,
    )

    assert fields == {
        "package_name": "kvrocks",
        "source_repo_url": (
            "https://github.com/apache/kvrocks/tree/v2.16.0"
        ),
        "domain": "Database",
        "os_version": "24.03-LTS-SP4",
        "app_version": "2.16.0",
    }


def test_parse_minimal_issue_can_take_version_from_pinned_source_url():
    fields = parse_issue_request(
        "【new-image】add kvrocks docker image on openEuler 24.03-LTS-SP4",
        MINIMAL_TARGET_ISSUE,
    )

    assert fields["app_version"] == "2.16.0"


def test_parse_minimal_issue_rejects_title_and_body_package_mismatch():
    with pytest.raises(IssueParseError, match="package"):
        parse_issue_request(
            "【new-image】add nginx 1.27.2 docker image on openEuler 24.03-LTS-SP4",
            MINIMAL_TARGET_ISSUE,
        )


def test_validate_accepts_the_target_repository_three_field_body():
    assert validate(parse_issue_body(MINIMAL_TARGET_ISSUE)) == []
