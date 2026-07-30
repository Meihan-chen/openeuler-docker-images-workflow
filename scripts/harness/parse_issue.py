"""Parse a GitCode Issue body to extract new image request parameters."""

import json
import os
import re
import sys
from urllib.parse import urlparse


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


def _version_from_source_url(source_url: str) -> str:
    path = [part for part in urlparse(source_url).path.split("/") if part]
    for marker in ("tree", "tag"):
        if marker not in path:
            continue
        index = path.index(marker)
        if index + 1 < len(path):
            version = path[index + 1]
            if version.startswith("v") and len(version) > 1:
                version = version[1:]
            return version
    raise IssueParseError(
        "version is missing from the Issue title and pinned source URL"
    )


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

    app_version = (
        request_parts[1]
        if len(request_parts) == 2
        else _version_from_source_url(fields["source_repo_url"])
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
