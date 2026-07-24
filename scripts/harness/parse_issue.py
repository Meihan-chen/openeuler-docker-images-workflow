"""Parse a GitCode Issue body to extract new image request parameters."""

import json
import os
import re
import sys
from typing import Optional


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

    patterns = {
        "package_name": r"\*\*Package Name\*\*[:\s]*(.+)",
        "source_repo_url": r"\*\*Upstream Repository\*\*[:\s]*(.+)",
        "domain": r"\*\*Domain\*\*[:\s]*(.+)",
        "os_version": r"\*\*openEuler Version\*\*[:\s]*(.+)",
        "app_version": r"\*\*App Version\*\*[:\s]*(.+)",
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            fields[key] = match.group(1).strip()

    # Derive domain from domain name to directory
    domain_map = {
        "ai": "AI", "bigdata": "Bigdata", "big data": "Bigdata",
        "storage": "Storage", "database": "Database", "cloud": "Cloud",
        "distroless": "Distroless", "hpc": "HPC", "others": "Others",
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
    required = ["package_name", "source_repo_url", "domain", "os_version"]
    return [f for f in required if f not in fields]


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