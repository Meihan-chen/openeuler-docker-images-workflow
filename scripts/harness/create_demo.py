"""Create demo files for testing the PR pipeline - no LLM needed.

Used as a fallback when DEEPSEEK_API_KEY is not configured.
Generates a minimal but valid image directory: Dockerfile, meta.yml, README.md,
doc/image-info.yml, logo, tests/goss.yaml + test.sh entry.
"""

import json
import os
from pathlib import Path


def create_demo(app: str, version: str, os_ver: str, domain: str) -> str:
    os_tag = "oe" + os_ver.lower().replace(".", "").replace("-", "")

    base = Path(domain) / app
    ver_dir = base / version / os_ver
    doc_dir = base / "doc" / "picture"
    tests_dir = base / "tests"

    for d in [ver_dir, doc_dir, tests_dir, base / "results" / version / os_ver]:
        d.mkdir(parents=True, exist_ok=True)

    # Dockerfile - uses ${app} package; works for many openEuler-packaged apps
    (ver_dir / "Dockerfile").write_text(f"""ARG BASE=openeuler/openeuler:{os_ver}
FROM ${{BASE}}

ARG VERSION={version}

RUN dnf install -y {app} && dnf clean all

EXPOSE 80
STOPSIGNAL SIGQUIT
CMD ["{app}", "-g", "daemon off;"]
""")

    # meta.yml
    (base / "meta.yml").write_text(f"""{version}-{os_tag}:
  path: {version}/{os_ver}/Dockerfile
""")

    # README.md
    (base / "README.md").write_text(f"""# Quick reference
- The official {app} docker image.
- Maintained by: [openEuler](https://atomgit.com/openeuler)

# {app} | openEuler
Current {app} images are built on [openEuler](https://repo.openeuler.org/).

# Supported tags and respective dockerfile links
| Tag | Currently | Architectures |
|-----|-----------|---------------|
| [{version}-{os_tag}](https://atomgit.com/openeuler/openeuler-docker-images/blob/master/{domain}/{app}/{version}/{os_ver}/Dockerfile) | {app} {version} on openEuler {os_ver} | amd64, arm64 |

# Usage
- Pull the `openeuler/{app}` image:
	```
	docker pull openeuler/{app}:{{{{Tag}}}}
	```
- Start:
	```
	docker run -d --name my-{app} -p 80:80 openeuler/{app}:{{{{Tag}}}}
	```
- View logs:
	```
	docker logs -f my-{app}
	```
- Interactive shell:
	```
	docker exec -it my-{app} /bin/bash
	```

# Question and answering
If you have any questions, please submit an issue on [openeuler-docker-images](https://atomgit.com/openeuler/openeuler-docker-images).
""")

    # image-info.yml
    (doc_dir.parent / "image-info.yml").write_text(f"""name: {app}
category: {domain.lower()}
description: {app} container image based on openEuler.
license: BSD-2-Clause
environment: |
  本应用在Docker环境中运行，安装Docker执行如下命令
  ```
  yum install -y docker
  ```
tags: |
  | Tag | Currently | Architectures |
  |-----|-----------|---------------|
  |[{version}-{os_tag}](https://atomgit.com/openeuler/openeuler-docker-images/blob/master/{domain}/{app}/{version}/{os_ver}/Dockerfile) | {app} {version} on openEuler {os_ver} | amd64, arm64 |
download: |
  ```
  docker pull openeuler/{app}:{{{{Tag}}}}
  ```
usage: |
  启动容器：
  ```
  docker run -d --name my-{app} -p 80:80 openeuler/{app}:{{{{Tag}}}}
  ```
similar_packages:
  - Apache HTTP Server: Apache 基金会的 HTTP 服务器
  - Caddy: 自动 HTTPS 的现代 Web 服务器
  - HAProxy: 高性能 TCP/HTTP 负载均衡器
dependency:
  - pcre
  - openssl
homepage: https://github.com/{app}/{app}
upstream:
  backend: GitHub
  version_url: {app}/{app}
  version_filter: alpha;rc;candidate;beta;pre
  version_scheme: RPM
""")

    # logo placeholder (1x1 PNG)
    (doc_dir / "logo.png").write_bytes(
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
    )

    # tests/goss.yaml - runtime assertions (per DESIGN §5.3)
    (tests_dir / "goss.yaml").write_text(f"""file:
  /usr/sbin/{app}:
    exists: true
    mode: "0755"
package:
  {app}:
    installed: true
port:
  tcp:80:
    listening: true
""")

    # tests/goss_wait.yaml - readiness wait conditions
    (tests_dir / "goss_wait.yaml").write_text("""port:
  tcp:80:
    listening: true
    timeout: 30000
""")

    # tests/test_helpers.sh
    (tests_dir / "test_helpers.sh").write_text("""wait_for_port() {
    local port=$1 timeout=${2:-30}
    for _ in $(seq 1 "$timeout"); do
        if ss -tlnp 2>/dev/null | grep -q ":$port "; then return 0; fi
        sleep 1
    done
    return 1
}
""")

    # test.sh entry alongside Dockerfile (per DESIGN §5.3)
    (ver_dir / "test.sh").write_text(f"""#!/bin/bash
set -e; set -o pipefail

CONTAINER_NAME="${{CONTAINER_NAME:-${{PACKAGE_NAME:-{app}}}-test}}"
BINARY="{app}"
EXPECTED_VERSION="{version}"

test_binary_exists() {{
    if docker exec "${{CONTAINER_NAME}}" which "$BINARY" >/dev/null 2>&1; then
        echo "PASS: binary exists"; return 0
    fi
    echo "FAIL: binary not found"; return 1
}}

test_version() {{
    local output
    output=$(docker exec "${{CONTAINER_NAME}}" "$BINARY" -v 2>&1 || echo "")
    if echo "$output" | grep -qi "$EXPECTED_VERSION"; then
        echo "PASS: version check - $output"; return 0
    fi
    echo "FAIL: version check - expected $EXPECTED_VERSION, got: $output"; return 1
}}

main() {{
    local failures=0
    test_binary_exists || failures=$((failures + 1))
    test_version || failures=$((failures + 1))
    if [ "$failures" -eq 0 ]; then
        echo "ALL_TESTS_PASSED"; exit 0
    fi
    echo "TESTS_FAILED: $failures failures"; exit 1
}}
main "$@"
""")

    # ai-result.json
    (base / "ai-result.json").write_text(
        json.dumps({
            "success": True,
            "package_name": app,
            "version": version,
            "files_created": [
                f"{domain}/{app}/{version}/{os_ver}/Dockerfile",
                f"{domain}/{app}/meta.yml",
                f"{domain}/{app}/README.md",
                f"{domain}/{app}/doc/image-info.yml",
                f"{domain}/{app}/doc/picture/logo.png",
                f"{domain}/{app}/tests/goss.yaml",
                f"{domain}/{app}/tests/goss_wait.yaml",
                f"{domain}/{app}/tests/test_helpers.sh",
                f"{domain}/{app}/{version}/{os_ver}/test.sh",
            ],
        }, ensure_ascii=False, indent=2)
    )

    # Update image-list.yml
    il = Path(domain) / "image-list.yml"
    if not il.exists():
        il.write_text(f"images:\n  {app}: {app}\n")
    elif app not in il.read_text():
        with il.open("a") as f:
            f.write(f"  {app}: {app}\n")

    # Dummy test results (so compose-pr has something to show in dry runs)
    results = base / "results" / version / os_ver
    for arch in ["amd64", "arm64"]:
        (results / f"{arch}.junit.xml").write_text(
            f'<?xml version="1.0"?><testsuite name="{app}" tests="2" failures="0">'
            f'<testcase name="binary_exists"/><testcase name="version_check"/></testsuite>'
        )

    return str(base)


if __name__ == "__main__":
    app = os.environ.get("PACKAGE", os.environ.get("APP", "nginx"))
    ver = os.environ.get("APP_VERSION", "1.27.2")
    os_ver = os.environ.get("OS_VERSION", "24.03-lts")
    domain = os.environ.get("DOMAIN", "Cloud")
    path = create_demo(app, ver, os_ver, domain)
    print(f"Demo files created at {path}")
