"""Tests for parse_issue.py."""

import json
import sys
import urllib.error
import urllib.request
from io import BytesIO

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


def test_title_version_takes_precedence_over_a_different_source_ref(monkeypatch):
    def unexpected_urlopen(request, timeout):
        pytest.fail(f"latest release lookup was not expected: {request.full_url}")

    monkeypatch.setattr(urllib.request, "urlopen", unexpected_urlopen)

    fields = parse_issue_request(
        "【new-image】add kvrocks 3.0.0 docker image on openEuler 24.03-LTS-SP4",
        MINIMAL_TARGET_ISSUE,
    )

    assert fields["app_version"] == "3.0.0"
    assert fields["source_repo_url"].endswith("/tree/v2.16.0")


def test_parse_minimal_issue_uses_latest_github_release_or_tag_when_version_is_omitted(
    monkeypatch,
):
    requested_urls = []

    def fake_urlopen(request, timeout):
        requested_urls.append((request.full_url, timeout))
        payload = (
            {"tag_name": "Percona-Server-5.5.55-38.8"}
            if request.full_url.endswith("/releases/latest")
            else [
                {"ref": "refs/tags/12.4"},
                {"ref": "refs/tags/mysqlsummit-0.2.1"},
                {"ref": "refs/tags/Percona-Server-8.4.10-10"},
                {"ref": "refs/tags/Percona-Server-9.7.1-1"},
                {"ref": "refs/tags/version_where_test_case_for_bug_31581_works"},
            ]
        )
        return BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    body = MINIMAL_TARGET_ISSUE.replace(
        "https://github.com/apache/kvrocks/tree/v2.16.0",
        "https://github.com/percona/percona-server",
    ).replace("kvrocks", "percona-server")

    fields = parse_issue_request(
        "【new-image】add percona-server docker image on openEuler 24.03-LTS-SP4",
        body,
    )

    assert fields["app_version"] == "9.7.1-1"
    assert fields["source_repo_url"] == (
        "https://github.com/percona/percona-server/tree/Percona-Server-9.7.1-1"
    )
    assert requested_urls == [
        (
            "https://api.github.com/repos/percona/percona-server/releases/latest",
            30,
        ),
        (
            "https://api.github.com/repos/percona/percona-server/"
            "git/matching-refs/tags/",
            30,
        ),
    ]


def test_parse_minimal_issue_falls_back_to_latest_github_tag(monkeypatch):
    requested_urls = []

    def fake_urlopen(request, timeout):
        requested_urls.append(request.full_url)
        if request.full_url.endswith("/releases/latest"):
            raise urllib.error.HTTPError(
                request.full_url,
                404,
                "Not Found",
                {},
                None,
            )
        return BytesIO(json.dumps([{"ref": "refs/tags/v9.1.0"}]).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    body = MINIMAL_TARGET_ISSUE.replace(
        "https://github.com/apache/kvrocks/tree/v2.16.0",
        "https://github.com/example/no-releases",
    ).replace("kvrocks", "example-app")

    fields = parse_issue_request(
        "【new-image】add example-app docker image on openEuler 24.03-LTS-SP4",
        body,
    )

    assert fields["app_version"] == "9.1.0"
    assert fields["source_repo_url"] == (
        "https://github.com/example/no-releases/tree/v9.1.0"
    )
    assert requested_urls == [
        "https://api.github.com/repos/example/no-releases/releases/latest",
        "https://api.github.com/repos/example/no-releases/git/matching-refs/tags/",
    ]


def test_parse_minimal_issue_rejects_title_and_body_package_mismatch():
    with pytest.raises(IssueParseError, match="package"):
        parse_issue_request(
            "【new-image】add nginx 1.27.2 docker image on openEuler 24.03-LTS-SP4",
            MINIMAL_TARGET_ISSUE,
        )


def test_validate_accepts_the_target_repository_three_field_body():
    assert validate(parse_issue_body(MINIMAL_TARGET_ISSUE)) == []
