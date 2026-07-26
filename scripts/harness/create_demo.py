"""Create demo files for testing the PR pipeline — no LLM needed."""

import os
import sys
from pathlib import Path


def create_demo(app: str, version: str, os_ver: str, domain: str) -> str:
    os_tag = "oe" + os_ver.lower().replace(".", "").replace("-", "")

    base = Path(domain) / app
    ver_dir = base / version / os_ver
    doc_dir = base / "doc" / "picture"

    for d in [ver_dir, doc_dir, base / "tests"]:
        d.mkdir(parents=True, exist_ok=True)

    # Dockerfile
    (ver_dir / "Dockerfile").write_text(f"""ARG BASE=openeuler/openeuler:{os_ver}
FROM ${{BASE}}

ARG VERSION={version}

RUN dnf install -y nginx && dnf clean all

EXPOSE 80
STOPSIGNAL SIGQUIT
CMD ["nginx", "-g", "daemon off;"]
""")

    # meta.yml
    (base / "meta.yml").write_text(f"""{version}-{os_tag}:
  path: {version}/{os_ver}/Dockerfile
""")

    # README.md
    (base / "README.md").write_text(f"""# Quick reference
- The official nginx docker image.
- Maintained by: [openEuler](https://atomgit.com/openeuler)

# nginx | openEuler
Current nginx images are built on [openEuler](https://repo.openeuler.org/).

# Supported tags and respective dockerfile links
| Tag | Currently | Architectures |
|-----|-----------|---------------|
| [{version}-{os_tag}](https://atomgit.com/openeuler/openeuler-docker-images/blob/master/{domain}/{app}/{version}/{os_ver}/Dockerfile) | nginx {version} on openEuler {os_ver.upper()} | amd64, arm64 |

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
description: nginx 是一款高性能的 HTTP 和反向代理服务器。
license: BSD-2-Clause
environment: |
  本应用在Docker环境中运行，安装Docker执行如下命令
  ```
  yum install -y docker
  ```
tags: |
  | Tag | Currently | Architectures |
  |-----|-----------|---------------|
  |[{version}-{os_tag}](https://atomgit.com/openeuler/openeuler-docker-images/blob/master/{domain}/{app}/{version}/{os_ver}/Dockerfile) | nginx {version} on openEuler {os_ver} | amd64, arm64 |
download: |
  ```
  docker pull openeuler/{app}:{{{{Tag}}}}
  ```
usage: |
  启动容器：
  ```
  docker run -d --name my-{app} -p 80:80 openeuler/{app}:{{{{Tag}}}}
  ```
license: BSD-2-Clause
similar_packages:
  - Apache HTTP Server: Apache 基金会的 HTTP 服务器
  - Caddy: 自动 HTTPS 的现代 Web 服务器
  - HAProxy: 高性能 TCP/HTTP 负载均衡器
dependency:
  - pcre
  - openssl
homepage: https://github.com/nginx/nginx
upstream:
  backend: GitHub
  version_url: nginx/nginx
  version_filter: alpha;rc;candidate;beta;pre
  version_scheme: RPM
""")

    # logo placeholder
    (doc_dir / "logo.png").write_bytes(
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
    )

    # test.sh
    (ver_dir / "test.sh").write_text(f"""#!/bin/bash
set -e
echo "PASS: binary exists" && echo "PASS: version check" && echo "ALL_TESTS_PASSED"
""")

    # ai-result.json
    (base / "ai-result.json").write_text(f'{{"success": true, "package_name": "{app}", "version": "{version}", "files_created": ["{domain}/{app}/{version}/{os_ver}/Dockerfile", "{domain}/{app}/meta.yml", "{domain}/{app}/README.md"]}}')

    # Update image-list.yml
    il = Path(domain) / "image-list.yml"
    if not il.exists():
        il.write_text(f"images:\n  {app}: {app}\n")
    elif app not in il.read_text():
        with il.open("a") as f:
            f.write(f"  {app}: {app}\n")

    # Dummy test results
    results = base / "results" / version / os_ver
    results.mkdir(parents=True, exist_ok=True)
    for arch in ["linux/amd64", "linux/arm64"]:
        pf = arch.replace("/", "_")
        (results / f"{pf}.junit.xml").write_text(f'<?xml version="1.0"?><testsuite name="{app}" tests="3" failures="0"><testcase name="version_check"/><testcase name="binary_exists"/><testcase name="functional"/></testsuite>')

    return str(base)


if __name__ == "__main__":
    app = os.environ.get("PACKAGE", os.environ.get("APP", "nginx"))
    ver = os.environ.get("APP_VERSION", "1.27.2")
    os_ver = os.environ.get("OS_VERSION", "24.03-lts")
    domain = os.environ.get("DOMAIN", "Cloud")
    path = create_demo(app, ver, os_ver, domain)
    print(f"Demo files created at {path}")