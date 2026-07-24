"""Query anitya via projectsInfoUrl for version comparison data.

Usage:
    python query_version.py [--app <name>] [--check-oe] [--format json|text]
"""

import argparse
import json
import os
import sys
from typing import Optional

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_projects_info_url() -> str:
    return os.environ.get(
        "PROJECTS_INFO_URL",
        "https://easysoftware-monitoring.test.osinfra.cn/api/projects/",
    )


def _load_backup_url() -> str:
    return os.environ.get(
        "BACKUP_PROJECTS_INFO_URL",
        "https://easysoftware-monitoring.test.osinfra.cn/api/projects/backup/",
    )


def _load_oe_versions_url() -> str:
    return os.environ.get(
        "OE_VERSIONS_URL",
        "https://datastat.openeuler.org/query/versions?community=openeuler",
    )


def _http_get(url: str) -> Optional[dict]:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "openeuler-autopilot/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"HTTP error for {url}: {e}", file=sys.stderr)
        return None


def _parse_meta_yml(app_dir: str) -> list[str]:
    """Parse meta.yml to get existing versions for an app."""
    meta_path = os.path.join(app_dir, "meta.yml")
    if not os.path.exists(meta_path):
        return []
    with open(meta_path) as f:
        data = yaml.safe_load(f) or {}
    return list(data.keys())


def _parse_image_info(app_dir: str) -> Optional[dict]:
    """Parse doc/image-info.yml for upstream info."""
    info_path = os.path.join(app_dir, "doc", "image-info.yml")
    if not os.path.exists(info_path):
        return None
    with open(info_path) as f:
        return yaml.safe_load(f) or {}


def query_app_version(app_name: str) -> Optional[dict]:
    """Query projectsInfoUrl for a single app's version comparison."""
    url = _load_projects_info_url() + app_name
    result = _http_get(url)

    if not result or not result.get("items"):
        backup_url = _load_backup_url() + app_name
        result = _http_get(backup_url)

    if not result or not result.get("items"):
        return None

    info = {"app_name": app_name}
    for item in result["items"]:
        tag = item.get("tag", "")
        if tag == "app_up":
            info["upstream_version"] = item.get("version", "").replace("_", ".")
        elif tag == "app_openeuler":
            info["oe_version"] = item.get("version", "")
            raw = item.get("raw_versions", [])
            if raw:
                v = info["oe_version"]
                matched = [str(r) for r in raw if v in str(r)]
                if matched:
                    info["os_version"] = str(matched[0]).split(v + "-")[-1] if "-" in str(matched[0]) else ""

    return info


def check_all_apps(app_name: Optional[str] = None) -> list[dict]:
    """Check all apps for version updates, or a specific app."""
    domains = ["AI", "Bigdata", "Storage", "Database", "Cloud", "Distroless", "HPC", "Others"]
    updates = []

    for domain in domains:
        domain_path = os.path.join(PROJECT_ROOT, domain)
        if not os.path.isdir(domain_path):
            continue
        for app in os.listdir(domain_path):
            app_dir = os.path.join(domain_path, app)
            if not os.path.isdir(app_dir):
                continue
            if app_name and app != app_name:
                continue

            existing = _parse_meta_yml(app_dir)
            if not existing:
                continue

            result = query_app_version(app)
            if not result:
                continue

            upstream = result.get("upstream_version", "")
            oe_ver = result.get("oe_version", "")
            os_ver = result.get("os_version", "")

            if upstream and upstream != oe_ver:
                tag = f"{upstream}-oe{os_ver.replace('.', '').replace('-', '')}" if os_ver else ""
                if tag not in existing:
                    updates.append({
                        "name": app,
                        "version": upstream,
                        "os_version": os_ver,
                        "domain": domain,
                    })

    return updates


def check_oe_version() -> dict:
    """Check for new openEuler OS versions."""
    url = _load_oe_versions_url()
    result = _http_get(url)
    if not result or not result.get("data"):
        return {"version": "", "apps": []}

    latest = result["data"][0]
    apps = []
    domains = ["AI", "Bigdata", "Storage", "Database", "Cloud", "Distroless", "HPC", "Others"]

    for domain in domains:
        domain_path = os.path.join(PROJECT_ROOT, domain)
        if not os.path.isdir(domain_path):
            continue
        for app in os.listdir(domain_path):
            app_dir = os.path.join(domain_path, app)
            if not os.path.isdir(app_dir):
                continue
            existing = _parse_meta_yml(app_dir)
            if existing:
                apps.append(app)

    return {"version": latest, "apps": apps}


def main() -> None:
    parser = argparse.ArgumentParser(description="Query anitya version data")
    parser.add_argument("--app", help="Specific app to check")
    parser.add_argument("--check-oe", action="store_true", help="Check for new openEuler version")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    args = parser.parse_args()

    if args.check_oe:
        result = check_oe_version()
    else:
        result = check_all_apps(args.app)

    if args.format == "json":
        print(json.dumps(result))
    else:
        for item in (result if isinstance(result, list) else [result]):
            print(json.dumps(item))


if __name__ == "__main__":
    main()