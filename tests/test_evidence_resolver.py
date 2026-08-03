import hashlib

import pytest


def _task(scenario):
    from scripts.lib.task_spec import TaskSpec

    return TaskSpec(
        app="example",
        version="1.2.3",
        os_version="24.03-lts-sp4",
        domain="Cloud",
        source_url="https://github.com/acme/example/tree/v1.2.3",
        scenario=scenario,
    )


def _request(**overrides):
    request = {
        "id": "command-status-001",
        "claim": "STATUS reports the application status",
        "source": (
            "https://github.com/acme/example/blob/v1.2.3/docs/commands.md"
        ),
        "locator": "STATUS command",
    }
    request.update(overrides)
    return request


@pytest.mark.parametrize("scenario", ["new-image", "version-update", "oe-upgrade"])
def test_resolves_and_hashes_pinned_upstream_evidence_for_every_scenario(
    scenario,
):
    from scripts.lib.evidence_resolver import resolve_evidence_requests

    content = (
        b"Introduction\n\nSTATUS command\nReturns the current server status.\n"
    )
    calls = []

    def fetcher(url, *, max_bytes, timeout):
        calls.append((url, max_bytes, timeout))
        return content

    bundle = resolve_evidence_requests(
        task=_task(scenario),
        requests=[_request()],
        fetcher=fetcher,
    )

    assert bundle["status"] == "resolved"
    assert bundle["scenario"] == scenario
    assert bundle["entries"][0]["resolved"] is True
    assert "Returns the current server status" in bundle["entries"][0]["excerpt"]
    assert bundle["entries"][0]["sha256"] == hashlib.sha256(content).hexdigest()
    assert bundle["entries"][0]["canonical_source"] == (
        "https://raw.githubusercontent.com/acme/example/"
        "v1.2.3/docs/commands.md"
    )
    assert calls == [
        (
            (
                "https://raw.githubusercontent.com/acme/example/"
                "v1.2.3/docs/commands.md"
            ),
            262_144,
            10,
        )
    ]


def test_empty_evidence_request_list_is_not_requested():
    from scripts.lib.evidence_resolver import resolve_evidence_requests

    bundle = resolve_evidence_requests(
        task=_task("new-image"),
        requests=[],
        fetcher=lambda *_args, **_kwargs: pytest.fail("must not fetch"),
    )

    assert bundle["status"] == "not_requested"
    assert bundle["entries"] == []


def test_non_list_evidence_requests_are_rejected():
    from scripts.lib.evidence_resolver import resolve_evidence_requests

    bundle = resolve_evidence_requests(
        task=_task("new-image"),
        requests={"id": "not-a-list"},
        fetcher=lambda *_args, **_kwargs: pytest.fail("must not fetch"),
    )

    assert bundle["status"] == "rejected"


def test_rejects_cross_repository_evidence_without_fetching_it():
    from scripts.lib.evidence_resolver import resolve_evidence_requests

    calls = []

    def fetcher(url, **_):
        calls.append(url)
        return b"should not be fetched"

    bundle = resolve_evidence_requests(
        task=_task("new-image"),
        requests=[
            _request(
                source=(
                    "https://github.com/other/project/blob/v1.2.3/commands.md"
                )
            )
        ],
        fetcher=fetcher,
    )

    assert bundle["status"] == "unresolved"
    assert bundle["entries"][0]["reason"] == "source is outside TaskSpec upstream"
    assert calls == []


def test_marks_missing_locator_unresolved_instead_of_accepting_whole_page():
    from scripts.lib.evidence_resolver import resolve_evidence_requests

    bundle = resolve_evidence_requests(
        task=_task("oe-upgrade"),
        requests=[_request(locator="missing symbol")],
        fetcher=lambda *_args, **_kwargs: b"unrelated documentation",
    )

    assert bundle["status"] == "unresolved"
    assert bundle["entries"][0]["resolved"] is False
    assert bundle["entries"][0]["reason"] == "locator was not found"


def test_rejects_unpinned_source_url():
    from scripts.lib.evidence_resolver import resolve_evidence_requests

    bundle = resolve_evidence_requests(
        task=_task("version-update"),
        requests=[
            _request(
                source="https://github.com/acme/example/blob/main/docs/commands.md"
            )
        ],
        fetcher=lambda *_args, **_kwargs: b"STATUS command",
    )

    assert bundle["status"] == "unresolved"
    assert bundle["entries"][0]["reason"] == "source is not pinned to TaskSpec revision"


def test_rejects_version_text_outside_the_repository_revision():
    from scripts.lib.evidence_resolver import resolve_evidence_requests

    calls = []
    bundle = resolve_evidence_requests(
        task=_task("new-image"),
        requests=[
            _request(
                source=(
                    "https://github.com/acme/example/blob/main/"
                    "docs/1.2.3/commands.md"
                )
            )
        ],
        fetcher=lambda url, **_: calls.append(url) or b"STATUS command",
    )

    assert bundle["status"] == "unresolved"
    assert bundle["entries"][0]["reason"] == "source is not pinned to TaskSpec revision"
    assert calls == []


def test_accepts_a_nonstandard_tag_when_it_exactly_matches_taskspec_revision():
    from scripts.lib.evidence_resolver import resolve_evidence_requests
    from scripts.lib.task_spec import TaskSpec

    task = TaskSpec(
        app="example",
        version="1.2.3",
        os_version="24.03-lts-sp4",
        domain="Cloud",
        source_url=(
            "https://github.com/acme/example/tree/release-1.2.3"
        ),
        scenario="new-image",
    )
    request = _request(
        source=(
            "https://github.com/acme/example/blob/"
            "release-1.2.3/docs/commands.md"
        )
    )

    bundle = resolve_evidence_requests(
        task=task,
        requests=[request],
        fetcher=lambda *_args, **_kwargs: b"STATUS command\nworks\n",
    )

    assert bundle["status"] == "resolved"


def test_rejects_more_than_twelve_requests_without_fetching():
    from scripts.lib.evidence_resolver import resolve_evidence_requests

    calls = []
    bundle = resolve_evidence_requests(
        task=_task("new-image"),
        requests=[_request(id=f"evidence-{index}") for index in range(13)],
        fetcher=lambda url, **_: calls.append(url) or b"STATUS command",
    )

    assert bundle["status"] == "rejected"
    assert "at most 12" in bundle["reason"]
    assert bundle["entries"] == []
    assert calls == []


def test_rejects_oversized_request_fields_without_fetching():
    from scripts.lib.evidence_resolver import resolve_evidence_requests

    calls = []
    bundle = resolve_evidence_requests(
        task=_task("new-image"),
        requests=[_request(claim="x" * 4001)],
        fetcher=lambda url, **_: calls.append(url) or b"STATUS command",
    )

    assert bundle["status"] == "rejected"
    assert "claim exceeds" in bundle["reason"]
    assert calls == []


def test_rejects_unapproved_evidence_hosts_before_dns_or_fetch():
    from scripts.lib.evidence_resolver import resolve_evidence_requests

    task = _task("new-image")
    object.__setattr__(
        task,
        "source_url",
        "https://evidence.example/acme/example/tree/v1.2.3",
    )
    calls = []
    bundle = resolve_evidence_requests(
        task=task,
        requests=[
            _request(
                source=(
                    "https://evidence.example/acme/example/blob/"
                    "v1.2.3/docs/commands.md"
                )
            )
        ],
        fetcher=lambda url, **_: calls.append(url) or b"STATUS command",
    )

    assert bundle["status"] == "unresolved"
    assert bundle["entries"][0]["reason"] == "evidence host is not supported"
    assert calls == []


def test_network_fetcher_enforces_the_host_allowlist_before_dns(monkeypatch):
    from scripts.lib import evidence_resolver

    dns_calls = []
    monkeypatch.setattr(
        evidence_resolver,
        "_public_host",
        lambda host: dns_calls.append(host),
    )

    with pytest.raises(ValueError, match="host is not supported"):
        evidence_resolver.fetch_evidence_source(
            "https://metadata.attacker.example/acme/example/"
            "blob/v1.2.3/README.md"
        )

    assert dns_calls == []


def test_stops_fetching_when_the_total_resolution_budget_is_exhausted():
    from scripts.lib.evidence_resolver import resolve_evidence_requests

    ticks = iter((0.0, 0.0, 46.0))
    calls = []
    bundle = resolve_evidence_requests(
        task=_task("new-image"),
        requests=[_request(id="first"), _request(id="second")],
        fetcher=lambda url, **_: calls.append(url) or b"STATUS command",
        clock=lambda: next(ticks),
    )

    assert bundle["status"] == "rejected"
    assert "time budget" in bundle["reason"]
    assert calls == [
        "https://raw.githubusercontent.com/acme/example/"
        "v1.2.3/docs/commands.md"
    ]


def test_rejects_a_bundle_that_exceeds_the_prompt_budget():
    from scripts.lib.evidence_resolver import resolve_evidence_requests

    bundle = resolve_evidence_requests(
        task=_task("new-image"),
        requests=[
            _request(id=f"evidence-{index}", claim="x" * 4000)
            for index in range(12)
        ],
        fetcher=lambda *_args, **_kwargs: b"STATUS command\nworks\n",
    )

    assert bundle["status"] == "rejected"
    assert "bundle exceeds" in bundle["reason"]
    assert bundle["entries"] == []
