"""Small, testable GitCode API boundary with fail-closed write controls."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping


TARGET_REPO = "openeuler/openeuler-docker-images"
TEST_PUSH_REPO = "qq_42020325/openeuler-docker-images"
TARGET_BRANCH = "master"


class DeliveryConfigError(ValueError):
    """Raised when delivery configuration could write to an unsafe target."""


def _required(raw: Mapping[str, object], field_name: str) -> str:
    value = str(raw.get(field_name, "")).strip()
    if not value:
        raise DeliveryConfigError(f"{field_name} is required")
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
                raise DeliveryConfigError(
                    "test delivery_mode must be validate_only or fork_pr"
                )
            if push_repo != TEST_PUSH_REPO:
                raise DeliveryConfigError(
                    "test push_repo must be the configured GitCode fork"
                )
            if guard_setting not in {"", "disabled"}:
                raise DeliveryConfigError("duplicate PR guard is disabled in test")
            duplicate_pr_guard_enabled = False
        # NOTE(production-delivery): this branch is fully implemented but
        # currently unreachable. No workflow can select it, because
        # flow.py _fork_deliver hard-codes the test pair above. Keep it in
        # sync when the delivery config becomes a parameter.
        elif environment == "production":
            if delivery_mode != "direct_branch_pr":
                raise DeliveryConfigError("production requires direct_branch_pr")
            if push_repo != TARGET_REPO:
                raise DeliveryConfigError(
                    "production push_repo must equal target_repo"
                )
            if guard_setting == "disabled":
                raise DeliveryConfigError(
                    "production duplicate PR guard cannot be disabled"
                )
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


class GitCodeClientError(RuntimeError):
    """Base error for GitCode delivery failures."""


class GitCodeAPIError(GitCodeClientError):
    """Raised for a non-successful GitCode API response."""


class GitCodeWriteForbiddenError(GitCodeClientError):
    """Raised when a delivery mode forbids the requested write."""


class DuplicatePullRequestError(GitCodeClientError):
    """Raised by the production duplicate-PR guard."""


@dataclass(frozen=True, repr=False)
class GitCodeRequest:
    method: str
    path: str
    params: Mapping[str, object] = field(default_factory=dict)
    json_body: Mapping[str, object] | None = None

    def __repr__(self) -> str:
        safe_params = {
            key: "REDACTED" if key == "access_token" else value
            for key, value in self.params.items()
        }
        return (
            "GitCodeRequest("
            f"method={self.method!r}, path={self.path!r}, "
            f"params={safe_params!r}, json_body={self.json_body!r})"
        )


@dataclass(frozen=True)
class GitCodeResponse:
    status: int
    payload: object


@dataclass(frozen=True)
class GitCodeResource:
    number: int | None
    url: str


Transport = Callable[[GitCodeRequest], GitCodeResponse]


def _decode_response(status: int, payload: bytes) -> GitCodeResponse:
    if not payload:
        return GitCodeResponse(status=status, payload={})
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        decoded = payload.decode("utf-8", errors="replace")
    return GitCodeResponse(status=status, payload=decoded)


class _UrllibTransport:
    def __init__(self, api_base: str, timeout: float):
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    def __call__(self, request: GitCodeRequest) -> GitCodeResponse:
        query = urllib.parse.urlencode(request.params)
        url = f"{self.api_base}{request.path}"
        if query:
            url = f"{url}?{query}"
        body = None
        if request.json_body is not None:
            body = json.dumps(request.json_body).encode("utf-8")
        http_request = urllib.request.Request(
            url,
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method=request.method,
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout) as response:
                return _decode_response(response.status, response.read())
        except urllib.error.HTTPError as error:
            return _decode_response(error.code, error.read())
        except urllib.error.URLError as error:
            return GitCodeResponse(status=0, payload={"message": str(error.reason)})


class GitCodeClient:
    def __init__(
        self,
        *,
        token: str,
        transport: Transport | None = None,
        api_base: str = "https://api.gitcode.com/api/v5",
        timeout: float = 30,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not token:
            raise ValueError("GitCode token is required")
        self._token = token
        self._transport = transport or _UrllibTransport(api_base, timeout)
        self._sleep = sleep

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> object:
        authenticated_params = {"access_token": self._token}
        if params:
            authenticated_params.update(params)
        request = GitCodeRequest(
            method=method,
            path=path,
            params=authenticated_params,
            json_body=json_body,
        )
        attempts = 0
        while True:
            attempts += 1
            response = self._transport(request)
            transient_read_failure = request.method == "GET" and (
                response.status in {0, 429} or response.status >= 500
            )
            if not transient_read_failure or attempts >= 3:
                break
            self._sleep(2 ** (attempts - 1))
        if response.status < 200 or response.status >= 300:
            detail = json.dumps(response.payload, ensure_ascii=False)[:1000]
            detail = detail.replace(self._token, "REDACTED")
            raise GitCodeAPIError(
                f"GitCode API {method} {path} failed with status "
                f"{response.status}: {detail}"
            )
        return response.payload

    @staticmethod
    def _split_repo(target_repo: str) -> tuple[str, str]:
        owner, separator, repo = target_repo.partition("/")
        if not separator or not owner or not repo or "/" in repo:
            raise ValueError("target_repo must be owner/repository")
        return owner, repo

    @staticmethod
    def _resource(payload: object) -> GitCodeResource:
        if not isinstance(payload, Mapping):
            raise GitCodeAPIError("GitCode API response must be an object")
        number = payload.get("number")
        if number is None:
            number = payload.get("iid")
        url = (
            payload.get("web_url")
            or payload.get("html_url")
            or payload.get("url")
            or ""
        )
        return GitCodeResource(
            number=int(number) if number is not None else None,
            url=str(url),
        )

    def _find_open_pull_request(
        self,
        *,
        repo: str,
        base: str,
        title: str,
    ) -> GitCodeResource | None:
        page = 1
        while True:
            payload = self._request(
                "GET",
                f"/repos/{repo}/pulls",
                params={
                    "state": "open",
                    "base": base,
                    "page": page,
                    "per_page": 100,
                },
            )
            if not isinstance(payload, list):
                raise GitCodeAPIError("GitCode pull request list must be an array")
            for pull_request in payload:
                if (
                    isinstance(pull_request, Mapping)
                    and pull_request.get("title") == title
                ):
                    return self._resource(pull_request)
            if len(payload) < 100:
                return None
            page += 1

    def create_pull_request(
        self,
        *,
        config: DeliveryConfig,
        title: str,
        body: str,
        branch: str,
        issue_id: str = "",
    ) -> GitCodeResource:
        if not config.allows_pr_create:
            raise GitCodeWriteForbiddenError(
                f"delivery mode {config.delivery_mode} forbids PR creation"
            )

        if config.duplicate_pr_guard_enabled:
            duplicate = self._find_open_pull_request(
                repo=config.target_repo,
                base=config.target_branch,
                title=title,
            )
            if duplicate is not None:
                raise DuplicatePullRequestError(
                    f"open pull request already exists: #{duplicate.number} "
                    f"{duplicate.url}"
                )

        json_body = {
            "title": title,
            "head": config.pr_head(branch),
            "base": config.target_branch,
            "body": body,
        }
        if issue_id:
            if not issue_id.isdigit() or int(issue_id) <= 0:
                raise ValueError("issue_id must be a positive integer")
            json_body["issue"] = issue_id
            json_body["close_related_issue"] = False

        payload = self._request(
            "POST",
            f"/repos/{config.target_repo}/pulls",
            json_body=json_body,
        )
        return self._resource(payload)

    def create_issue(
        self,
        *,
        target_repo: str,
        title: str,
        body: str,
        labels: str = "",
    ) -> GitCodeResource:
        owner, repo = self._split_repo(target_repo)
        json_body = {"repo": repo, "title": title, "body": body}
        if labels:
            json_body["labels"] = labels
        payload = self._request(
            "POST",
            f"/repos/{owner}/issues",
            json_body=json_body,
        )
        return self._resource(payload)

    def list_issues(
        self,
        *,
        target_repo: str,
        state: str = "all",
        search: str = "",
    ) -> list[Mapping[str, object]]:
        if state not in {"open", "closed", "all"}:
            raise ValueError("issue state must be open, closed or all")
        owner, repo = self._split_repo(target_repo)
        page = 1
        issues: list[Mapping[str, object]] = []
        while True:
            params: dict[str, object] = {
                "state": state,
                "page": page,
                "per_page": 100,
            }
            if search:
                params["search"] = search
            payload = self._request(
                "GET",
                f"/repos/{owner}/{repo}/issues",
                params=params,
            )
            if not isinstance(payload, list):
                raise GitCodeAPIError("GitCode issue list must be an array")
            if any(not isinstance(issue, Mapping) for issue in payload):
                raise GitCodeAPIError("GitCode issue list contains a non-object")
            issues.extend(payload)
            if len(payload) < 100:
                return issues
            page += 1

    def get_issue(
        self,
        *,
        target_repo: str,
        number: int,
    ) -> Mapping[str, object]:
        if number <= 0:
            raise ValueError("issue number must be positive")
        owner, repo = self._split_repo(target_repo)
        payload = self._request(
            "GET",
            f"/repos/{owner}/{repo}/issues/{number}",
        )
        if not isinstance(payload, Mapping):
            raise GitCodeAPIError("GitCode issue response must be an object")
        return payload

    def update_issue(
        self,
        *,
        target_repo: str,
        number: int,
        title: str,
        body: str,
        state: str,
        labels: str = "",
        issue_status: str = "",
    ) -> GitCodeResource:
        if number <= 0:
            raise ValueError("issue number must be positive")
        if state not in {"open", "closed"}:
            raise ValueError("issue state must be open or closed")
        owner, repo = self._split_repo(target_repo)
        json_body = {
            "repo": repo,
            "title": title,
            "body": body,
            "state": state,
        }
        if labels:
            json_body["labels"] = labels
        if issue_status:
            json_body["status"] = issue_status
        payload = self._request(
            "PATCH",
            f"/repos/{owner}/issues/{number}",
            json_body=json_body,
        )
        return self._resource(payload)

    def create_issue_comment(
        self,
        *,
        target_repo: str,
        number: int,
        body: str,
    ) -> Mapping[str, object]:
        if number <= 0:
            raise ValueError("issue number must be positive")
        owner, repo = self._split_repo(target_repo)
        payload = self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            json_body={"body": body},
        )
        if not isinstance(payload, Mapping):
            raise GitCodeAPIError("GitCode issue comment response must be an object")
        return payload
