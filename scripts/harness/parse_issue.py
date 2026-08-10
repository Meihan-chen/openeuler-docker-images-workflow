"""Parse a GitCode Issue body to extract new image request parameters."""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import datetime
from urllib.parse import quote, urlparse


class IssueParseError(ValueError):
    """Raised when a new-image Issue cannot form a complete request."""


_FIELD_PATTERNS = {
    "package_name": (
        r"Package Name",
        r"软件包名称\s*[（(]\s*Package Name\s*[）)]",
    ),
    "source_repo_url": (
        r"Upstream Repository",
        r"Source Repository",
        r"源码仓库\s*[（(]\s*Source Repository\s*[）)]",
    ),
    "domain": (
        r"Domain",
        r"所属领域\s*[（(]\s*Domain\s*[）)]",
    ),
    "os_version": (r"openEuler Version",),
    "app_version": (r"App Version",),
}

_TITLE_RE = re.compile(
    r"^\s*【new-image】\s*add\s+(?P<request>.+?)\s+docker\s+image\s+"
    r"on\s+openEuler\s+(?P<os_version>\d{2}\.\d{2}"
    r"(?:-lts)?(?:-sp\d+)?)\s*$",
    re.IGNORECASE,
)
_TAG_VERSION_RE = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)+(?:[A-Za-z][A-Za-z0-9]*)?"
    r"(?:[-+._][A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)*)"
)


def parse_issue_body(body: str) -> dict:
    """Extract structured fields from a new-image issue body.

    Expected format (from issue template):
        **Package Name**: nginx
        **Upstream Repository**: https://github.com/nginx/nginx
        **Domain**: Cloud
        **openEuler Version**: 24.03-lts
        **App Version**: 1.27.2
    """
    fields = {}

    for key, labels in _FIELD_PATTERNS.items():
        label_pattern = "|".join(f"(?:{label})" for label in labels)
        pattern = (
            rf"^\s*\*\*(?:{label_pattern})\s*[：:]?\*\*"
            rf"\s*[：:]?\s*(.+?)\s*$"
        )
        match = re.search(pattern, body, re.IGNORECASE | re.MULTILINE)
        if match and match.group(1).strip():
            fields[key] = match.group(1).strip()

    # Derive domain from domain name to directory
    domain_map = {
        "ai": "AI", "bigdata": "Bigdata", "big data": "Bigdata",
        "storage": "Storage", "database": "Database", "cloud": "Cloud",
        "distroless": "Distroless", "hpc": "HPC", "others": "Others",
        "人工智能": "AI", "大数据": "Bigdata", "存储": "Storage",
        "数据库": "Database", "云计算": "Cloud", "高性能计算": "HPC",
        "其他": "Others", "虚拟化": "Cloud", "云原生": "Cloud",
        "网络": "Cloud", "机器学习": "AI", "安全": "Security",
        "virtualization": "Cloud", "cloudnative": "Cloud",
        "network": "Cloud", "ml": "AI", "db": "Database",
        "security": "Security",
    }
    if "domain" in fields:
        fields["domain"] = domain_map.get(
            fields["domain"].lower().strip(), fields["domain"]
        )

    # Derive OS tag
    if "os_version" in fields:
        tag = fields["os_version"].lower().replace(".", "").replace("-", "")
        if not tag.startswith("oe"):
            tag = "oe" + tag
        fields["os_tag"] = tag

    return fields


def validate(fields: dict) -> list[str]:
    """Validate required fields, return list of missing fields."""
    required = ["package_name", "source_repo_url", "domain"]
    return [f for f in required if f not in fields]


def _version_from_tag(tag: str) -> str | None:
    versions = _TAG_VERSION_RE.findall(tag)
    if not versions:
        return None
    return versions[0]


def _version_from_source_url(source_url: str) -> str | None:
    path = [part for part in urlparse(source_url).path.split("/") if part]
    for marker in ("tree", "tag"):
        if marker not in path:
            continue
        index = path.index(marker)
        if index + 1 < len(path):
            return _version_from_tag(path[index + 1])
    return None


def _github_repository(source_url: str) -> tuple[str, str]:
    parsed = urlparse(source_url)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or len(parts) < 2
    ):
        raise IssueParseError(
            "automatic latest release lookup requires a GitHub repository URL"
        )
    owner = parts[0]
    repo = parts[1].removesuffix(".git")
    if not owner or not repo:
        raise IssueParseError(
            "automatic latest release lookup requires a GitHub repository URL"
        )
    return owner, repo


def _github_api_json(
    url: str,
    *,
    payload: Mapping[str, object] | None = None,
) -> object:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "openeuler-autopilot/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise IssueParseError(f"GitHub API lookup failed: {error}") from error
    except (OSError, ValueError, urllib.error.URLError) as error:
        raise IssueParseError(f"GitHub API lookup failed: {error}") from error


_LATEST_TAG_QUERY = """
query LatestVersionTag($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    refs(
      refPrefix: "refs/tags/"
      first: 100
      orderBy: {field: TAG_COMMIT_DATE, direction: DESC}
    ) {
      nodes {
        name
        target {
          ... on Commit {
            committedDate
          }
          ... on Tag {
            tagger {
              date
            }
            target {
              ... on Commit {
                committedDate
              }
            }
          }
        }
      }
    }
  }
}
"""


def _tag_timestamp(target: object) -> str:
    if not isinstance(target, Mapping):
        return ""
    tagger = target.get("tagger")
    if isinstance(tagger, Mapping) and tagger.get("date"):
        return str(tagger["date"])
    if target.get("committedDate"):
        return str(target["committedDate"])
    nested_target = target.get("target")
    if isinstance(nested_target, Mapping) and nested_target.get("committedDate"):
        return str(nested_target["committedDate"])
    return ""


def _latest_github_tag(owner: str, repo: str) -> tuple[str, str] | None:
    payload = _github_api_json(
        "https://api.github.com/graphql",
        payload={
            "query": _LATEST_TAG_QUERY,
            "variables": {"owner": owner, "repo": repo},
        },
    )
    if not isinstance(payload, Mapping) or payload.get("errors"):
        raise IssueParseError("GitHub latest tag lookup returned an error")
    data = payload.get("data")
    repository = data.get("repository") if isinstance(data, Mapping) else None
    refs = repository.get("refs") if isinstance(repository, Mapping) else None
    nodes = refs.get("nodes") if isinstance(refs, Mapping) else None
    if not isinstance(nodes, list):
        raise IssueParseError("GitHub latest tag lookup returned invalid data")
    tags = [
        (str(node.get("name", "")).strip(), _tag_timestamp(node.get("target")))
        for node in nodes
        if isinstance(node, Mapping) and str(node.get("name", "")).strip()
    ]
    if not tags:
        return None
    return next(
        (candidate for candidate in tags if _version_from_tag(candidate[0])),
        tags[0],
    )


def _timestamp_key(timestamp: str) -> float:
    if not timestamp:
        return float("-inf")
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


def _latest_github_release_or_tag(source_url: str) -> tuple[str, str]:
    owner, repo = _github_repository(source_url)
    release_payload = _github_api_json(
        f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    )
    candidates = []
    if isinstance(release_payload, Mapping):
        release_tag = str(release_payload.get("tag_name", "")).strip()
        if release_tag:
            candidates.append(
                (
                    release_tag,
                    str(
                        release_payload.get("published_at")
                        or release_payload.get("created_at")
                        or ""
                    ),
                )
            )
    latest_tag = _latest_github_tag(owner, repo)
    if latest_tag is not None:
        candidates.append(latest_tag)
    if not candidates:
        raise IssueParseError("GitHub repository has no release or tag")
    tag, _timestamp = max(
        candidates,
        key=lambda candidate: _timestamp_key(candidate[1]),
    )
    version = _version_from_tag(tag) or tag
    pinned_url = (
        f"https://github.com/{owner}/{repo}/tree/{quote(tag, safe='')}"
    )
    return version, pinned_url


def parse_issue_request(title: str, body: str) -> dict[str, str]:
    """Parse the target repository's minimal new-image Issue format."""

    fields = parse_issue_body(body)
    missing = validate(fields)
    if missing:
        raise IssueParseError(
            f"missing required fields: {', '.join(missing)}"
        )

    match = _TITLE_RE.fullmatch(title)
    if match is None:
        raise IssueParseError(
            "title must match 【new-image】add <app> [version] docker image "
            "on openEuler <version>"
        )
    request_parts = match.group("request").split()
    if len(request_parts) not in {1, 2}:
        raise IssueParseError("title package or version is ambiguous")
    title_package = request_parts[0]
    if title_package.lower() != fields["package_name"].lower():
        raise IssueParseError("package in title and body must match")

    if len(request_parts) == 2:
        app_version = request_parts[1]
    else:
        app_version = _version_from_source_url(fields["source_repo_url"])
        if app_version is None:
            app_version, fields["source_repo_url"] = _latest_github_release_or_tag(
                fields["source_repo_url"]
            )
    return {
        "package_name": fields["package_name"],
        "source_repo_url": fields["source_repo_url"],
        "domain": fields["domain"],
        "os_version": match.group("os_version"),
        "app_version": app_version,
    }


def main() -> None:
    if len(sys.argv) < 2:
        # Try reading from GITHUB_EVENT_PATH
        event_path = os.environ.get("GITHUB_EVENT_PATH")
        if event_path:
            with open(event_path) as f:
                event = json.load(f)
            body = event.get("issue", {}).get("body", "")
        else:
            body = sys.stdin.read()
    else:
        body = sys.argv[1]

    fields = parse_issue_body(body)
    missing = validate(fields)

    if missing:
        print(f"::error::Missing required fields: {', '.join(missing)}")
        sys.exit(1)

    # Output as GitHub Actions output format
    for key, value in fields.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
