import pytest


TARGET_REPO = "openeuler/openeuler-docker-images"
BRANCH = "auto/new-image/kvrocks/2.16.0-oe2403sp4"


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def _delivery_config(environment="test"):
    from scripts.lib.gitcode_client import DeliveryConfig

    if environment == "test":
        return DeliveryConfig.from_mapping(
            {
                "environment": "test",
                "delivery_mode": "fork_pr",
                "target_repo": TARGET_REPO,
                "push_repo": "qq_42020325/openeuler-docker-images",
                "target_branch": "master",
            }
        )
    return DeliveryConfig.from_mapping(
        {
            "environment": "production",
            "delivery_mode": "direct_branch_pr",
            "target_repo": TARGET_REPO,
            "push_repo": TARGET_REPO,
            "target_branch": "master",
        }
    )


def test_test_delivery_creates_cross_repo_pr_without_duplicate_lookup():
    from scripts.utils.gitcode import GitCodeClient, GitCodeResponse

    transport = RecordingTransport(
        [
            GitCodeResponse(
                status=201,
                payload={
                    "number": 42,
                    "html_url": "https://gitcode.com/openeuler/openeuler-docker-images/pull/42",
                },
            )
        ]
    )
    client = GitCodeClient(token="top-secret", transport=transport)
    config = _delivery_config()

    result = client.create_pull_request(
        config=config,
        title="[New Image] Add Apache Kvrocks 2.16.0",
        body="Validated on x86_64 and aarch64.",
        branch=BRANCH,
    )

    assert result.number == 42
    assert result.url.endswith("/pull/42")
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.path == "/repos/openeuler/openeuler-docker-images/pulls"
    assert request.params == {"access_token": "top-secret"}
    assert request.json_body == {
        "title": "[New Image] Add Apache Kvrocks 2.16.0",
        "head": f"qq_42020325:{BRANCH}",
        "base": "master",
        "body": "Validated on x86_64 and aarch64.",
    }


def test_production_delivery_checks_every_pr_page_before_rejecting_duplicate():
    from scripts.utils.gitcode import (
        DuplicatePullRequestError,
        GitCodeClient,
        GitCodeResponse,
    )

    title = "[New Image] Add Apache Kvrocks 2.16.0"
    first_page = [{"title": f"other-{index}"} for index in range(100)]
    transport = RecordingTransport(
        [
            GitCodeResponse(status=200, payload=first_page),
            GitCodeResponse(
                status=200,
                payload=[
                    {
                        "title": title,
                        "iid": 73,
                        "web_url": (
                            "https://gitcode.com/openeuler/"
                            "openeuler-docker-images/pull/73"
                        ),
                    }
                ],
            ),
        ]
    )
    client = GitCodeClient(token="top-secret", transport=transport)

    with pytest.raises(DuplicatePullRequestError, match="73"):
        client.create_pull_request(
            config=_delivery_config("production"),
            title=title,
            body="body",
            branch=BRANCH,
        )

    assert [request.method for request in transport.requests] == ["GET", "GET"]
    assert transport.requests[0].params["page"] == 1
    assert transport.requests[1].params["page"] == 2


def test_create_issue_uses_official_owner_endpoint_and_repo_body_field():
    from scripts.utils.gitcode import GitCodeClient, GitCodeResponse

    transport = RecordingTransport(
        [
            GitCodeResponse(
                status=201,
                payload={
                    "iid": 9,
                    "web_url": (
                        "https://gitcode.com/openeuler/"
                        "openeuler-docker-images/issues/9"
                    ),
                },
            )
        ]
    )
    client = GitCodeClient(token="top-secret", transport=transport)

    result = client.create_issue(
        target_repo=TARGET_REPO,
        title="[E2E TEST] controlled issue contract run-123",
        body="Create, update and close contract probe.",
        labels="e2e-test,automation",
    )

    assert result.number == 9
    assert result.url.endswith("/issues/9")
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.path == "/repos/openeuler/issues"
    assert request.params == {"access_token": "top-secret"}
    assert request.json_body == {
        "repo": "openeuler-docker-images",
        "title": "[E2E TEST] controlled issue contract run-123",
        "body": "Create, update and close contract probe.",
        "labels": "e2e-test,automation",
    }


def test_response_aliases_are_normalized():
    from scripts.utils.gitcode import GitCodeClient, GitCodeResponse

    transport = RecordingTransport(
        [
            GitCodeResponse(
                status=201,
                payload={"iid": 7, "url": "https://gitcode.com/example/7"},
            )
        ]
    )
    client = GitCodeClient(token="top-secret", transport=transport)

    result = client.create_issue(
        target_repo=TARGET_REPO,
        title="failure",
        body="body",
    )

    assert result.number == 7
    assert result.url == "https://gitcode.com/example/7"


def test_api_error_and_request_repr_redact_access_token():
    from scripts.utils.gitcode import (
        GitCodeAPIError,
        GitCodeClient,
        GitCodeRequest,
        GitCodeResponse,
    )

    token = "token-must-never-appear"
    request = GitCodeRequest(
        method="GET",
        path="/repos/openeuler/openeuler-docker-images/pulls",
        params={"access_token": token, "state": "open"},
    )
    assert token not in repr(request)
    assert "REDACTED" in repr(request)

    transport = RecordingTransport(
        [
            GitCodeResponse(
                status=401,
                payload={"message": f"invalid access token {token}"},
            )
        ]
    )
    client = GitCodeClient(token=token, transport=transport)

    with pytest.raises(GitCodeAPIError) as error:
        client.create_issue(
            target_repo=TARGET_REPO,
            title="failure",
            body="body",
        )

    assert token not in str(error.value)
    assert "REDACTED" in str(error.value)


def test_validate_only_cannot_create_pr_even_with_a_client():
    from scripts.lib.gitcode_client import DeliveryConfig
    from scripts.utils.gitcode import GitCodeClient, GitCodeWriteForbiddenError

    config = DeliveryConfig.from_mapping(
        {
            "environment": "test",
            "delivery_mode": "validate_only",
            "target_repo": TARGET_REPO,
            "push_repo": "qq_42020325/openeuler-docker-images",
            "target_branch": "master",
        }
    )
    transport = RecordingTransport([])
    client = GitCodeClient(token="top-secret", transport=transport)

    with pytest.raises(GitCodeWriteForbiddenError):
        client.create_pull_request(
            config=config,
            title="must not be created",
            body="body",
            branch=BRANCH,
        )

    assert transport.requests == []


def test_list_issues_preserves_arrays_and_paginates():
    from scripts.utils.gitcode import GitCodeClient, GitCodeResponse

    first_page = [
        {
            "number": index,
            "title": f"issue-{index}",
            "html_url": f"https://gitcode.com/example/issues/{index}",
        }
        for index in range(100)
    ]
    transport = RecordingTransport(
        [
            GitCodeResponse(status=200, payload=first_page),
            GitCodeResponse(
                status=200,
                payload=[
                    {
                        "number": 101,
                        "title": "matching issue",
                        "html_url": "https://gitcode.com/example/issues/101",
                    }
                ],
            ),
        ]
    )
    client = GitCodeClient(token="top-secret", transport=transport)

    issues = client.list_issues(
        target_repo=TARGET_REPO,
        state="open",
        search="new-image-database-kvrocks",
    )

    assert len(issues) == 101
    assert issues[-1]["title"] == "matching issue"
    assert transport.requests[0].path == (
        "/repos/openeuler/openeuler-docker-images/issues"
    )
    assert transport.requests[0].params["page"] == 1
    assert transport.requests[1].params["page"] == 2
    assert transport.requests[1].params["search"] == (
        "new-image-database-kvrocks"
    )


def test_update_and_close_issue_use_official_owner_endpoint():
    from scripts.utils.gitcode import GitCodeClient, GitCodeResponse

    transport = RecordingTransport(
        [
            GitCodeResponse(
                status=200,
                payload={
                    "number": 9,
                    "html_url": (
                        "https://gitcode.com/openeuler/"
                        "openeuler-docker-images/issues/9"
                    ),
                },
            )
        ]
    )
    client = GitCodeClient(token="top-secret", transport=transport)

    result = client.update_issue(
        target_repo=TARGET_REPO,
        number=9,
        title="[E2E TEST] updated contract run-123",
        body="Update succeeded; closing the controlled probe.",
        state="closed",
    )

    assert result.number == 9
    request = transport.requests[0]
    assert request.method == "PATCH"
    assert request.path == "/repos/openeuler/issues/9"
    assert request.json_body == {
        "repo": "openeuler-docker-images",
        "title": "[E2E TEST] updated contract run-123",
        "body": "Update succeeded; closing the controlled probe.",
        "state": "closed",
    }


def test_get_retries_transient_failures_but_post_does_not():
    from scripts.utils.gitcode import GitCodeAPIError, GitCodeClient, GitCodeResponse

    sleeps = []
    get_transport = RecordingTransport(
        [
            GitCodeResponse(status=429, payload={"message": "slow down"}),
            GitCodeResponse(status=503, payload={"message": "unavailable"}),
            GitCodeResponse(status=200, payload=[]),
        ]
    )
    client = GitCodeClient(
        token="top-secret",
        transport=get_transport,
        sleep=sleeps.append,
    )

    assert client.list_issues(target_repo=TARGET_REPO) == []
    assert len(get_transport.requests) == 3
    assert sleeps == [1, 2]

    post_transport = RecordingTransport(
        [GitCodeResponse(status=503, payload={"message": "ambiguous write"})]
    )
    client = GitCodeClient(
        token="top-secret",
        transport=post_transport,
        sleep=lambda _: None,
    )

    with pytest.raises(GitCodeAPIError, match="ambiguous write"):
        client.create_issue(
            target_repo=TARGET_REPO,
            title="do not retry",
            body="body",
        )

    assert len(post_transport.requests) == 1


def test_create_issue_comment_uses_repository_issue_endpoint():
    from scripts.utils.gitcode import GitCodeClient, GitCodeResponse

    transport = RecordingTransport(
        [
            GitCodeResponse(
                status=201,
                payload={"id": 88, "body": "contract update"},
            )
        ]
    )
    client = GitCodeClient(token="top-secret", transport=transport)

    result = client.create_issue_comment(
        target_repo=TARGET_REPO,
        number=9,
        body="contract update",
    )

    assert result == {"id": 88, "body": "contract update"}
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.path == (
        "/repos/openeuler/openeuler-docker-images/issues/9/comments"
    )
    assert request.json_body == {"body": "contract update"}


def test_legacy_gitcode_cli_uses_the_shared_safe_client(monkeypatch):
    from scripts.utils import gitcode

    calls = []

    class SharedClient:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def _request(self, method, path, **kwargs):
            calls.append(("request", method, path, kwargs))
            return {"ok": True}

    monkeypatch.setenv("GITCODE_TOKEN", "secret")
    monkeypatch.setattr(gitcode, "GitCodeClient", SharedClient)

    result = gitcode._api_request(
        "GET",
        "/repos/openeuler/images/pulls?state=open&page=2",
    )

    assert result == {"ok": True}
    assert calls == [
        ("init", {"token": "secret"}),
        (
            "request",
            "GET",
            "/repos/openeuler/images/pulls",
            {"params": {"state": "open", "page": "2"}, "json_body": None},
        ),
    ]
