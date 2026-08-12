import hashlib

import pytest


def test_invocation_options_expand_all_scope_in_stable_order():
    from scripts.lib.oe_upgrade_contract import InvocationOptions

    options = InvocationOptions.from_mapping(
        {
            "tracking_issue_number": 123,
            "oe_version": "26.03-LTS",
            "scope": "all",
            "mode": "plan",
        }
    )

    assert options.oe_version == "26.03-lts"
    assert options.scope == (
        "AI",
        "Bigdata",
        "Cloud",
        "Database",
        "Distroless",
        "HPC",
        "Others",
        "Storage",
    )
    assert options.mode == "plan"


def test_upgrade_request_is_stable_and_validates_external_key():
    from scripts.lib.oe_upgrade_contract import UpgradeContractError, UpgradeRequest

    request = UpgradeRequest.create(
        tracking_issue_number=123,
        oe_version="26.03-lts",
        scope=("Database",),
        base_sha="a" * 40,
    )

    expected_key = hashlib.sha256(b"oe-upgrade\x00123\x0026.03-lts").hexdigest()[:16]
    assert request.request_key == expected_key
    assert UpgradeRequest.from_json(request.to_json()) == request

    forged = request.to_dict()
    forged["request_key"] = "0" * 16
    with pytest.raises(UpgradeContractError, match="request_key"):
        UpgradeRequest.from_mapping(forged)


def test_issue_parser_requires_matching_title_and_body_versions():
    from scripts.lib.oe_upgrade_contract import UpgradeContractError, parse_upgrade_issue

    title = "【oe-upgrade】upgrade latest application images to openEuler 26.03-LTS"
    body = "**openEuler 目标版本（Target openEuler Version）：** 24.03-lts-sp4\n"

    with pytest.raises(UpgradeContractError, match="title.*body"):
        parse_upgrade_issue(123, title, body, mode="deliver")


def test_issue_parser_accepts_minimal_body_and_optional_scope():
    from scripts.lib.oe_upgrade_contract import parse_upgrade_issue

    options = parse_upgrade_issue(
        123,
        "【oe-upgrade】upgrade latest application images to openEuler 26.03-LTS",
        (
            "**openEuler 目标版本（Target openEuler Version）：** 26.03-lts\n"
            "**Scope：** Database\n"
        ),
        mode="deliver",
    )

    assert options.tracking_issue_number == 123
    assert options.oe_version == "26.03-lts"
    assert options.scope == ("Database",)
    assert options.mode == "deliver"


def test_issue_parser_rejects_duplicate_target_version_field():
    from scripts.lib.oe_upgrade_contract import UpgradeContractError, parse_upgrade_issue

    with pytest.raises(UpgradeContractError, match="duplicate"):
        parse_upgrade_issue(
            123,
            "【oe-upgrade】upgrade latest application images to openEuler 26.03-LTS",
            (
                "**Target openEuler Version:** 26.03-lts\n"
                "**Target openEuler Version:** 26.03-lts\n"
            ),
            mode="plan",
        )
