"""Fail-closed delivery configuration for test and production repositories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


class DeliveryConfigError(ValueError):
    """Raised when delivery configuration could write to an unsafe target."""


TARGET_REPO = "openeuler/openeuler-docker-images"
TEST_PUSH_REPO = "qq_42020325/openeuler-docker-images"
TARGET_BRANCH = "master"


def _required(raw: Mapping[str, object], field: str) -> str:
    value = str(raw.get(field, "")).strip()
    if not value:
        raise DeliveryConfigError(f"{field} is required")
    return value


@dataclass(frozen=True)
class DeliveryConfig:
    environment: str
    delivery_mode: str
    target_repo: str
    push_repo: str
    target_branch: str
    duplicate_pr_guard_enabled: bool

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "DeliveryConfig":
        environment = _required(raw, "environment")
        delivery_mode = _required(raw, "delivery_mode")
        target_repo = _required(raw, "target_repo")
        push_repo = _required(raw, "push_repo")
        target_branch = _required(raw, "target_branch")

        if target_repo != TARGET_REPO:
            raise DeliveryConfigError("target_repo is not allowlisted")
        if target_branch != TARGET_BRANCH:
            raise DeliveryConfigError("target_branch must be master")

        guard_setting = str(raw.get("duplicate_pr_guard", "")).strip().lower()
        if environment == "test":
            if delivery_mode not in {"validate_only", "fork_pr"}:
                raise DeliveryConfigError("test delivery_mode must be validate_only or fork_pr")
            if push_repo != TEST_PUSH_REPO:
                raise DeliveryConfigError("test push_repo must be the configured GitCode fork")
            if guard_setting not in {"", "disabled"}:
                raise DeliveryConfigError("duplicate PR guard is disabled in test")
            duplicate_pr_guard_enabled = False
        elif environment == "production":
            if delivery_mode != "direct_branch_pr":
                raise DeliveryConfigError("production requires direct_branch_pr")
            if push_repo != TARGET_REPO:
                raise DeliveryConfigError("production push_repo must equal target_repo")
            if guard_setting == "disabled":
                raise DeliveryConfigError("production duplicate PR guard cannot be disabled")
            duplicate_pr_guard_enabled = True
        else:
            raise DeliveryConfigError("environment must be test or production")

        return cls(
            environment=environment,
            delivery_mode=delivery_mode,
            target_repo=target_repo,
            push_repo=push_repo,
            target_branch=target_branch,
            duplicate_pr_guard_enabled=duplicate_pr_guard_enabled,
        )

    @property
    def allows_branch_push(self) -> bool:
        return self.delivery_mode != "validate_only"

    @property
    def allows_pr_create(self) -> bool:
        return self.delivery_mode != "validate_only"

    def pr_head(self, branch: str) -> str:
        if not branch.startswith("auto/"):
            raise DeliveryConfigError("working branch must be under auto/")
        if self.push_repo == self.target_repo:
            return branch
        owner = self.push_repo.split("/", maxsplit=1)[0]
        return f"{owner}:{branch}"
