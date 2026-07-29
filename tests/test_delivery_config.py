import pytest


def _test_mapping(mode="validate_only"):
    return {
        "environment": "test",
        "delivery_mode": mode,
        "target_repo": "openeuler/openeuler-docker-images",
        "push_repo": "qq_42020325/openeuler-docker-images",
        "target_branch": "master",
    }


def test_validate_only_forbids_gitcode_writes():
    from scripts.lib.delivery_config import DeliveryConfig

    config = DeliveryConfig.from_mapping(_test_mapping())

    assert config.allows_branch_push is False
    assert config.allows_pr_create is False
    assert config.duplicate_pr_guard_enabled is False


def test_test_fork_pr_uses_cross_repository_head():
    from scripts.lib.delivery_config import DeliveryConfig

    config = DeliveryConfig.from_mapping(_test_mapping("fork_pr"))

    assert config.allows_branch_push is True
    assert config.allows_pr_create is True
    assert config.pr_head("auto/new-image/kvrocks/2.16.0-oe2403sp4") == (
        "qq_42020325:auto/new-image/kvrocks/2.16.0-oe2403sp4"
    )
    assert config.duplicate_pr_guard_enabled is False


def test_production_direct_mode_requires_target_repository_push():
    from scripts.lib.delivery_config import DeliveryConfig

    config = DeliveryConfig.from_mapping(
        {
            "environment": "production",
            "delivery_mode": "direct_branch_pr",
            "target_repo": "openeuler/openeuler-docker-images",
            "push_repo": "openeuler/openeuler-docker-images",
            "target_branch": "master",
        }
    )

    assert config.pr_head("auto/new-image/kvrocks/2.16.0-oe2403sp4") == (
        "auto/new-image/kvrocks/2.16.0-oe2403sp4"
    )
    assert config.duplicate_pr_guard_enabled is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"environment": "staging"},
        {"delivery_mode": "direct_branch_pr"},
        {"target_repo": "someone/other-repo"},
        {"push_repo": "openeuler/openeuler-docker-images"},
        {"target_branch": "main"},
    ],
)
def test_test_configuration_fails_closed(overrides):
    from scripts.lib.delivery_config import DeliveryConfig, DeliveryConfigError

    raw = _test_mapping("fork_pr")
    raw.update(overrides)

    with pytest.raises(DeliveryConfigError):
        DeliveryConfig.from_mapping(raw)


def test_production_rejects_disabled_duplicate_pr_guard():
    from scripts.lib.delivery_config import DeliveryConfig, DeliveryConfigError

    raw = {
        "environment": "production",
        "delivery_mode": "direct_branch_pr",
        "target_repo": "openeuler/openeuler-docker-images",
        "push_repo": "openeuler/openeuler-docker-images",
        "target_branch": "master",
        "duplicate_pr_guard": "disabled",
    }

    with pytest.raises(DeliveryConfigError, match="duplicate"):
        DeliveryConfig.from_mapping(raw)
