import hashlib

import pytest


def _task(scenario="new-image"):
    from scripts.lib.task_spec import TaskSpec

    return TaskSpec(
        app="example",
        version="1.2.3",
        os_version="24.03-lts-sp4",
        domain="Cloud",
        source_url="https://github.com/acme/example/tree/v1.2.3",
        scenario=scenario,
    )


def _evidence(**overrides):
    item = {
        "id": "command-status-001",
        "claim": "STATUS reports the application status",
        "source": "https://github.com/acme/example/blob/v1.2.3/docs/commands.md",
        "excerpts": ["STATUS command"],
    }
    item.update(overrides)
    return item


@pytest.mark.parametrize("scenario", ["new-image", "version-update", "oe-upgrade"])
def test_freezes_and_hashes_creator_evidence_for_every_scenario(scenario):
    from scripts.lib.evidence_resolver import freeze_creator_evidence

    content = b"STATUS command\nReturns the current server status.\n"
    calls = []

    def fetcher(url, *, max_bytes, timeout):
        calls.append((url, max_bytes, timeout))
        return content

    bundle = freeze_creator_evidence(
        task=_task(scenario),
        evidence=[_evidence()],
        fetcher=fetcher,
    )

    assert bundle["status"] == "available"
    assert bundle["scenario"] == scenario
    entry = bundle["entries"][0]
    assert entry["fetch_status"] == "available"
    assert entry["sha256"] == hashlib.sha256(content).hexdigest()
    assert entry["claim"] == "STATUS reports the application status"
    assert entry["excerpts"] == ["STATUS command"]
    assert entry["excerpt_checks"][0]["found"] is True
    assert calls[0][0] == (
        "https://raw.githubusercontent.com/acme/example/"
        "v1.2.3/docs/commands.md"
    )
    assert len(calls) == 1


def test_fetches_a_shared_source_once_and_checks_multiple_excerpts():
    from scripts.lib.evidence_resolver import freeze_creator_evidence

    content = (
        b"RUN groupadd --gid=999 -r example && \\\n"
        b"    useradd --uid=999 -r example\n\nUSER 999\n"
    )
    calls = []
    bundle = freeze_creator_evidence(
        task=_task(),
        evidence=[
            _evidence(
                id="identity-create",
                source="https://github.com/acme/example/blob/v1.2.3/Dockerfile",
                excerpts=[
                    "RUN groupadd --gid=999 -r example && \\\n"
                    "    useradd --uid=999 -r example",
                    "USER 999",
                ],
            ),
            _evidence(
                id="identity-runtime",
                source="https://github.com/acme/example/blob/v1.2.3/Dockerfile",
                excerpts=["USER 999"],
            ),
        ],
        fetcher=lambda url, **_: calls.append(url) or content,
    )

    assert bundle["status"] == "available"
    assert len(calls) == 1
    checks = bundle["entries"][0]["excerpt_checks"]
    assert [check["found"] for check in checks] == [True, True]
    assert checks[0]["match_method"] == "exact"


def test_empty_evidence_is_unavailable_without_fetching():
    from scripts.lib.evidence_resolver import freeze_creator_evidence

    bundle = freeze_creator_evidence(
        task=_task(),
        evidence=[],
        fetcher=lambda *_args, **_kwargs: pytest.fail("must not fetch"),
    )

    assert bundle["status"] == "unavailable"
    assert bundle["reason"] == "Creator provided no evidence"
    assert bundle["entries"] == []


def test_malformed_evidence_is_advisory_without_fetching():
    from scripts.lib.evidence_resolver import freeze_creator_evidence

    calls = []
    bundle = freeze_creator_evidence(
        task=_task(),
        evidence={"not": "a list"},
        fetcher=lambda url, **_: calls.append(url) or b"unexpected",
    )

    assert bundle["status"] == "unavailable"
    assert calls == []


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (
            "https://github.com/other/project/blob/v1.2.3/commands.md",
            "source is outside TaskSpec upstream",
        ),
        (
            "https://github.com/acme/example/blob/main/docs/1.2.3/commands.md",
            "source is not pinned to TaskSpec revision",
        ),
        (
            "https://metadata.attacker.example/acme/example/blob/v1.2.3/README.md",
            "evidence host is not supported",
        ),
    ],
)
def test_invalid_sources_are_advisory_and_not_fetched(source, reason):
    from scripts.lib.evidence_resolver import freeze_creator_evidence

    calls = []
    bundle = freeze_creator_evidence(
        task=_task(),
        evidence=[_evidence(source=source)],
        fetcher=lambda url, **_: calls.append(url) or b"unexpected",
    )

    assert bundle["status"] == "unavailable"
    assert bundle["entries"][0]["fetch_status"] == "unavailable"
    assert bundle["entries"][0]["reason"] == reason
    assert calls == []


def test_missing_excerpt_is_unavailable_but_preserves_fixed_source():
    from scripts.lib.evidence_resolver import freeze_creator_evidence

    content = b"real upstream content\n"
    bundle = freeze_creator_evidence(
        task=_task(),
        evidence=[_evidence(excerpts=["invented quote"])],
        fetcher=lambda *_args, **_kwargs: content,
    )

    assert bundle["status"] == "unavailable"
    entry = bundle["entries"][0]
    assert entry["fetch_status"] == "available"
    assert entry["sha256"] == hashlib.sha256(content).hexdigest()
    assert entry["excerpt_checks"] == [{"index": 0, "found": False}]


def test_accepts_a_nonstandard_exact_taskspec_revision():
    from scripts.lib.evidence_resolver import freeze_creator_evidence
    from scripts.lib.task_spec import TaskSpec

    task = TaskSpec(
        app="example",
        version="1.2.3",
        os_version="24.03-lts-sp4",
        domain="Cloud",
        source_url="https://github.com/acme/example/tree/release-1.2.3",
        scenario="new-image",
    )
    bundle = freeze_creator_evidence(
        task=task,
        evidence=[
            _evidence(
                source=(
                    "https://github.com/acme/example/blob/"
                    "release-1.2.3/docs/commands.md"
                )
            )
        ],
        fetcher=lambda *_args, **_kwargs: b"STATUS command\n",
    )

    assert bundle["status"] == "available"


def test_unpinned_taskspec_source_cannot_make_evidence_available():
    from scripts.lib.evidence_resolver import freeze_creator_evidence
    from scripts.lib.task_spec import TaskSpec

    task = TaskSpec(
        app="example",
        version="1.2.3",
        os_version="24.03-lts-sp4",
        domain="Cloud",
        source_url="https://github.com/acme/example",
        scenario="new-image",
    )
    calls = []

    bundle = freeze_creator_evidence(
        task=task,
        evidence=[_evidence(source="https://github.com/acme/example")],
        fetcher=lambda url, **_: calls.append(url) or b"STATUS command\n",
    )

    assert bundle["status"] == "unavailable"
    assert bundle["entries"][0]["fetch_status"] == "unavailable"
    assert bundle["entries"][0]["reason"] == (
        "TaskSpec source is not pinned to a revision"
    )
    assert calls == []


def test_revision_markers_in_owner_or_repo_names_cannot_bypass_pinning():
    from scripts.lib.evidence_resolver import freeze_creator_evidence
    from scripts.lib.task_spec import TaskSpec

    task = TaskSpec(
        app="v1",
        version="1.2.3",
        os_version="24.03-lts-sp4",
        domain="Cloud",
        source_url="https://github.com/tree/v1/tree/v1.2.3",
        scenario="new-image",
    )
    calls = []

    bundle = freeze_creator_evidence(
        task=task,
        evidence=[
            _evidence(
                source="https://github.com/tree/v1/blob/main/docs/commands.md"
            )
        ],
        fetcher=lambda url, **_: calls.append(url) or b"STATUS command\n",
    )

    assert bundle["status"] == "unavailable"
    assert bundle["entries"][0]["reason"] == (
        "source is not pinned to TaskSpec revision"
    )
    assert calls == []


def test_more_than_six_entries_is_advisory_and_not_fetched():
    from scripts.lib.evidence_resolver import freeze_creator_evidence

    calls = []
    bundle = freeze_creator_evidence(
        task=_task(),
        evidence=[_evidence(id=f"evidence-{index}") for index in range(7)],
        fetcher=lambda url, **_: calls.append(url) or b"STATUS command",
    )

    assert bundle["status"] == "unavailable"
    assert "at most 6" in bundle["reason"]
    assert calls == []


def test_more_than_two_excerpts_is_advisory_and_not_fetched():
    from scripts.lib.evidence_resolver import freeze_creator_evidence

    calls = []
    bundle = freeze_creator_evidence(
        task=_task(),
        evidence=[_evidence(excerpts=["one", "two", "three"])],
        fetcher=lambda url, **_: calls.append(url) or b"one two three\n",
    )

    assert bundle["status"] == "unavailable"
    assert bundle["entries"][0]["fetch_status"] == "unavailable"
    assert "1-2" in bundle["entries"][0]["reason"]
    assert calls == []


def test_oversized_or_duplicate_entries_are_advisory():
    from scripts.lib.evidence_resolver import freeze_creator_evidence

    bundle = freeze_creator_evidence(
        task=_task(),
        evidence=[
            _evidence(claim="x" * 513),
            _evidence(id="valid-entry"),
            _evidence(id="valid-entry"),
        ],
        fetcher=lambda *_args, **_kwargs: b"STATUS command\n",
    )

    assert bundle["status"] == "unavailable"
    assert bundle["entries"][0]["fetch_status"] == "unavailable"
    assert bundle["entries"][2]["reason"] == "evidence id is duplicated"


def test_excerpt_matching_is_exact_instead_of_fuzzy():
    from scripts.lib.evidence_resolver import freeze_creator_evidence

    bundle = freeze_creator_evidence(
        task=_task(),
        evidence=[_evidence(excerpts=["RUN install package enable feature"])],
        fetcher=lambda *_args, **_kwargs: (
            b"RUN install package \\\n    enable feature\n"
        ),
    )

    assert bundle["status"] == "unavailable"
    assert bundle["entries"][0]["excerpt_checks"] == [
        {"index": 0, "found": False}
    ]


def test_network_fetcher_rejects_unapproved_hosts_before_dns(monkeypatch):
    from scripts.lib import evidence_resolver

    dns_calls = []
    monkeypatch.setattr(
        evidence_resolver,
        "_public_host",
        lambda host: dns_calls.append(host),
    )

    with pytest.raises(ValueError, match="host is not supported"):
        evidence_resolver.fetch_evidence_source(
            "https://metadata.attacker.example/acme/example/blob/v1.2.3/README.md"
        )

    assert dns_calls == []


def test_exhausted_fetch_budget_degrades_to_unavailable():
    from scripts.lib.evidence_resolver import freeze_creator_evidence

    ticks = iter((0.0, 0.0, 46.0))
    calls = []
    bundle = freeze_creator_evidence(
        task=_task(),
        evidence=[
            _evidence(id="first"),
            _evidence(
                id="second",
                source="https://github.com/acme/example/blob/v1.2.3/README.md",
                excerpts=["README"],
            ),
        ],
        fetcher=lambda url, **_: calls.append(url) or b"STATUS command\n",
        clock=lambda: next(ticks),
    )

    assert bundle["status"] == "unavailable"
    assert bundle["entries"][1]["fetch_status"] == "unavailable"
    assert len(calls) == 1


def test_shared_advisory_resolution_keeps_creator_claim_when_resolver_fails():
    from scripts.lib.evidence_resolver import resolve_advisory_evidence

    def fail(**_kwargs):
        raise RuntimeError("network stack unavailable")

    bundle = resolve_advisory_evidence(
        task=_task(),
        scenario="new-image",
        evidence=[_evidence()],
        resolver=fail,
    )

    assert bundle["status"] == "unavailable"
    assert bundle["entries"][0]["claim"] == (
        "STATUS reports the application status"
    )
    assert bundle["entries"][0]["excerpts"] == ["STATUS command"]


def test_shared_qa_evidence_helpers_remove_duplicate_payload_content():
    from scripts.lib.evidence_resolver import (
        creator_result_for_qa,
        render_qa_evidence,
    )

    creator = {"success": True, "evidence": [_evidence()]}
    bundle = {
        "status": "available",
        "entries": [{"id": "command-status-001", "claim": "review me"}],
    }

    prepared = creator_result_for_qa(creator)
    section = render_qa_evidence(bundle)

    assert prepared["evidence"] == "See Harness-fixed evidence bundle below"
    assert creator["evidence"][0]["claim"] == (
        "STATUS reports the application status"
    )
    assert "Harness-fixed Creator evidence bundle" in section
    assert "review me" in section
    assert "must not create an issue" in section
