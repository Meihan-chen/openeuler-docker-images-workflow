"""GitCode API client wrapper.

GitCode API is compatible with GitHub API v3. Base URL: https://api.gitcode.com/api/v5/

Usage:
    python gitcode.py pr create --title "..." --body-file /tmp/pr-body.md --head "branch"
    python gitcode.py pr list --state open
    python gitcode.py issue create --title "..." --body-file /tmp/report.md --labels "label1,label2"
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error


BASE_URL = "https://api.gitcode.com/api/v5"


def _token() -> str:
    token = os.environ.get("GITCODE_TOKEN", "")
    if not token:
        print("::error::GITCODE_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    return token


def _repo() -> tuple[str, str]:
    """Return (owner, repo) from GITHUB_REPOSITORY env var."""
    repo = os.environ.get("GITHUB_REPOSITORY", "openeuler/openeuler-docker-images")
    owner, name = repo.split("/", 1)
    return owner, name


def _api_request(method: str, path: str, body: dict | None = None) -> dict:
    """Make an authenticated API request to GitCode."""
    url = f"{BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"::error::GitCode API error ({e.code}): {error_body}", file=sys.stderr)
        sys.exit(1)


def cmd_pr_create(args: argparse.Namespace) -> None:
    owner, repo = _repo()
    body_file = args.body_file
    body = open(body_file).read() if body_file else args.body or ""

    payload = {
        "title": args.title,
        "head": args.head,
        "base": args.base or "master",
        "body": body,
    }

    result = _api_request("POST", f"/repos/{owner}/{repo}/pulls", payload)
    print(f"PR created: {result.get('html_url', result.get('url', ''))}")


def cmd_pr_list(args: argparse.Namespace) -> None:
    owner, repo = _repo()
    page = 1
    all_prs = []
    while True:
        result = _api_request(
            "GET",
            f"/repos/{owner}/{repo}/pulls?state={args.state}&per_page=100&page={page}",
        )
        if not result:
            break
        all_prs.extend(result)
        if len(result) < 100:
            break
        page += 1

    for pr in all_prs:
        print(json.dumps({"title": pr["title"], "number": pr["number"], "state": pr["state"]}))


def cmd_issue_create(args: argparse.Namespace) -> None:
    owner, repo = _repo()
    body_file = args.body_file
    body = open(body_file).read() if body_file else args.body or ""

    payload = {
        "title": args.title,
        "body": body,
        "labels": args.labels.split(",") if args.labels else [],
    }

    result = _api_request("POST", f"/repos/{owner}/{repo}/issues", payload)
    print(f"Issue created: {result.get('html_url', result.get('url', ''))}")


def cmd_check_pr_exists(args: argparse.Namespace) -> None:
    """Check if a PR with the given title already exists. Exit 0 if found, 1 if not."""
    owner, repo = _repo()
    page = 1
    while True:
        result = _api_request(
            "GET",
            f"/repos/{owner}/{repo}/pulls?state=open&per_page=100&page={page}",
        )
        if not result:
            break
        for pr in result:
            if pr["title"] == args.title:
                print(f"PR already exists: #{pr['number']}")
                sys.exit(0)
        if len(result) < 100:
            break
        page += 1
    print("No existing PR found")
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="GitCode API client")
    sub = parser.add_subparsers(dest="resource")

    # PR commands
    pr = sub.add_parser("pr")
    pr_sub = pr.add_subparsers(dest="action")
    pr_create = pr_sub.add_parser("create")
    pr_create.add_argument("--title", required=True)
    pr_create.add_argument("--body", default="")
    pr_create.add_argument("--body-file")
    pr_create.add_argument("--head", required=True)
    pr_create.add_argument("--base", default="master")
    pr_list = pr_sub.add_parser("list")
    pr_list.add_argument("--state", default="open")
    pr_check = pr_sub.add_parser("check-exists")
    pr_check.add_argument("--title", required=True)

    # Issue commands
    issue = sub.add_parser("issue")
    issue_sub = issue.add_subparsers(dest="action")
    issue_create = issue_sub.add_parser("create")
    issue_create.add_argument("--title", required=True)
    issue_create.add_argument("--body", default="")
    issue_create.add_argument("--body-file")
    issue_create.add_argument("--labels", default="")

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