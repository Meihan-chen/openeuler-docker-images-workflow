"""GitCode API client (Gitea-style /api/v5).

Differences from GitHub API v3:
- Auth header: `PRIVATE-TOKEN: <token>` (not `Authorization: Bearer`)
- Git push URL: `https://oauth2:<TOKEN>@gitcode.com/...` (not `x-access-token`)
- Default branch on openeuler/openeuler-docker-images is `master`

Usage:
    python gitcode.py pr create --title "..." --body-file /tmp/pr-body.md --head "branch"
    python gitcode.py pr list --state open
    python gitcode.py pr check-exists --title "..."
    python gitcode.py issue create --title "..." --body-file /tmp/report.md --labels "l1,l2"
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error


BASE_URL = "https://api.gitcode.com/api/v5"
DEFAULT_REPO = "openeuler/openeuler-docker-images"


def _token() -> str:
    token = os.environ.get("GITCODE_TOKEN", "")
    if not token:
        print("::error::GITCODE_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    return token


def _repo() -> str:
    """Return 'owner/repo' for the target GitCode repository."""
    return os.environ.get("TARGET_REPO", DEFAULT_REPO)


def _api_request(method: str, path: str, body: dict | None = None) -> dict | list:
    """Make an authenticated API request to GitCode.

    Uses PRIVATE-TOKEN header (Gitea/GitCode style), not Bearer.
    """
    url = f"{BASE_URL}{path}"
    headers = {
        "PRIVATE-TOKEN": _token(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
            if not payload:
                return {}
            return json.loads(payload)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"::error::GitCode API error ({e.code}): {error_body}", file=sys.stderr)
        sys.exit(1)


def cmd_pr_create(args: argparse.Namespace) -> None:
    owner_repo = args.repo or _repo()
    body = open(args.body_file).read() if args.body_file else args.body or ""

    payload = {
        "title": args.title,
        "head": args.head,
        "base": args.base or "master",
        "body": body,
    }

    result = _api_request("POST", f"/repos/{owner_repo}/pulls", payload)
    pr_url = result.get("html_url") or result.get("url") or ""
    print(f"PR created: {pr_url}")
    if pr_url:
        with open("/tmp/pr-url", "w") as f:
            f.write(pr_url)


def cmd_pr_list(args: argparse.Namespace) -> None:
    owner_repo = args.repo or _repo()
    page = 1
    all_prs = []
    while True:
        result = _api_request(
            "GET",
            f"/repos/{owner_repo}/pulls?state={args.state}&per_page=100&page={page}",
        )
        if not result:
            break
        all_prs.extend(result)
        if len(result) < 100:
            break
        page += 1

    for pr in all_prs:
        print(json.dumps({
            "title": pr.get("title", ""),
            "number": pr.get("number"),
            "state": pr.get("state", ""),
            "html_url": pr.get("html_url", ""),
        }))


def cmd_issue_create(args: argparse.Namespace) -> None:
    owner_repo = args.repo or _repo()
    body = open(args.body_file).read() if args.body_file else args.body or ""

    payload = {
        "title": args.title,
        "body": body,
    }
    if args.labels:
        payload["labels"] = [{"name": l.strip()} for l in args.labels.split(",")]

    result = _api_request("POST", f"/repos/{owner_repo}/issues", payload)
    issue_url = result.get("html_url") or result.get("url") or ""
    print(f"Issue created: {issue_url}")


def cmd_check_pr_exists(args: argparse.Namespace) -> None:
    """Check if an open PR with the given title already exists.

    Exit 0 if found (PR exists), exit 1 if not found.
    Used by workflow to skip PR creation when a duplicate exists.
    """
    owner_repo = args.repo or _repo()
    page = 1
    while True:
        result = _api_request(
            "GET",
            f"/repos/{owner_repo}/pulls?state=open&per_page=100&page={page}",
        )
        if not result:
            break
        for pr in result:
            if pr.get("title") == args.title:
                print(f"PR already exists: #{pr.get('number')} - {pr.get('html_url', '')}")
                sys.exit(0)
        if len(result) < 100:
            break
        page += 1
    print("No existing PR found")
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="GitCode API client")
    sub = parser.add_subparsers(dest="resource")

    pr = sub.add_parser("pr")
    pr_sub = pr.add_subparsers(dest="action")
    pr_create = pr_sub.add_parser("create")
    pr_create.add_argument("--title", required=True)
    pr_create.add_argument("--body", default="")
    pr_create.add_argument("--body-file")
    pr_create.add_argument("--head", required=True)
    pr_create.add_argument("--base", default="master")
    pr_create.add_argument("--repo", default="")
    pr_list = pr_sub.add_parser("list")
    pr_list.add_argument("--state", default="open")
    pr_list.add_argument("--repo", default="")
    pr_check = pr_sub.add_parser("check-exists")
    pr_check.add_argument("--title", required=True)
    pr_check.add_argument("--repo", default="")

    issue = sub.add_parser("issue")
    issue_sub = issue.add_subparsers(dest="action")
    issue_create = issue_sub.add_parser("create")
    issue_create.add_argument("--title", required=True)
    issue_create.add_argument("--body", default="")
    issue_create.add_argument("--body-file")
    issue_create.add_argument("--labels", default="")
    issue_create.add_argument("--repo", default="")

    args = parser.parse_args()

    if args.resource == "pr" and args.action == "create":
        cmd_pr_create(args)
    elif args.resource == "pr" and args.action == "list":
        cmd_pr_list(args)
    elif args.resource == "pr" and args.action == "check-exists":
        cmd_check_pr_exists(args)
    elif args.resource == "issue" and args.action == "create":
        cmd_issue_create(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
