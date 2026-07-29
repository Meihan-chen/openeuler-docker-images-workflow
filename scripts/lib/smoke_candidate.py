"""Deterministic candidate used to exercise the pipeline without an AI call."""

from __future__ import annotations

from pathlib import Path

import yaml

from scripts.lib.progress import log
from scripts.lib.task_spec import TaskSpec


def write_smoke_candidate(
    *,
    workspace: Path,
    task: TaskSpec,
) -> dict[str, str]:
    workspace = Path(workspace)
    app = workspace / task.domain / task.app
    image = app / task.version / task.os_version
    tests = app / "tests"
    picture = app / "doc" / "picture"
    image.mkdir(parents=True)
    tests.mkdir(parents=True)
    picture.mkdir(parents=True)

    image_list = workspace / task.domain / "image-list.yml"
    image_list_data = yaml.safe_load(image_list.read_text())
    image_list_data["images"][task.app] = task.app
    image_list.write_text(
        yaml.safe_dump(image_list_data, sort_keys=False)
    )

    (app / "meta.yml").write_text(
        f"{task.version}-oe2403sp4:\n"
        f"  path: {task.version}/{task.os_version}/Dockerfile\n"
    )
    (app / "README.md").write_text(
        "# Quick reference\n\n"
        "# Kvrocks | openEuler\n\n"
        "# Supported tags and respective Dockerfile links\n\n"
        f"{task.version}-oe2403sp4\n\n"
        "# Usage\n\n"
        f"docker run openeuler/kvrocks:{task.version}-oe2403sp4\n\n"
        "# Question and answering\n"
    )
    (app / "doc" / "image-info.yml").write_text(
        "name: kvrocks\n"
        "category: database\n"
        "description: Apache Kvrocks key-value database.\n"
        "environment: Docker on openEuler\n"
        "tags: 2.16.0-oe2403sp4\n"
        "download: docker pull openeuler/kvrocks:{Tag}\n"
        "usage: docker run openeuler/kvrocks:{Tag}\n"
        "license: Apache-2.0\n"
        "similar_packages:\n"
        "  - Redis\n"
        "  - KeyDB\n"
        "  - Dragonfly\n"
        "dependency:\n"
        "  - N/A\n"
        "homepage: https://kvrocks.apache.org/\n"
        "upstream: https://github.com/apache/kvrocks\n"
    )
    (picture / "logo.png").write_bytes(
        b"\x89PNG\r\n\x1a\npipeline-smoke"
    )
    (image / "Dockerfile").write_text(
        f"ARG BASE=openeuler/openeuler:{task.os_version}\n"
        "FROM ${BASE} AS builder\n"
        f"ARG VERSION={task.version}\n"
        "WORKDIR /src/kvrocks\n"
        'RUN git clone --depth 1 --branch "v${VERSION}" '
        "https://github.com/apache/kvrocks.git . && ./x.py build -j 4\n"
        "FROM ${BASE}\n"
        "RUN groupadd --gid 999 kvrocks && "
        "useradd --uid 999 --gid kvrocks kvrocks && "
        "mkdir -p /var/lib/kvrocks && "
        "chown -R 999:999 /var/lib/kvrocks\n"
        "COPY --from=builder /src/kvrocks/build/kvrocks "
        "/usr/local/bin/kvrocks\n"
        "USER 999\n"
        "EXPOSE 6666\n"
        "HEALTHCHECK CMD redis-cli -p 6666 PING | grep PONG\n"
        'ENTRYPOINT ["kvrocks", "--bind", "0.0.0.0"]\n'
    )
    entry = image / "test.sh"
    entry.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        f"export EXPECTED_VERSION={task.version}\n"
        'exec ../../tests/test.sh "$@"\n'
    )
    (tests / "goss.yaml").write_text(
        "port:\n"
        "  tcp:6666:\n"
        "    listening: true\n"
        "command:\n"
        "  version:\n"
        "    exec: kvrocks --version\n"
        "    stdout:\n"
        '      - "{{.Env.EXPECTED_VERSION}}"\n'
        "  ping:\n"
        "    exec: redis-cli -p 6666 PING\n"
        "    stdout:\n"
        "      - PONG\n"
    )
    (tests / "goss_wait.yaml").write_text(
        "port:\n  tcp:6666:\n    listening: true\n"
    )
    helpers = tests / "test_helpers.sh"
    helpers.write_text(
        "#!/bin/bash\n"
        "wait_for_kvrocks() { redis-cli -p 6666 PING; }\n"
    )
    shared = tests / "test.sh"
    shared.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        ': "${EXPECTED_VERSION:?}"\n'
        'kvrocks --version | grep -F "${EXPECTED_VERSION}"\n'
        "redis-cli -p 6666 PING | grep -F PONG\n"
        'test "$(id -u)" = 999\n'
    )
    for script in (entry, helpers, shared):
        script.chmod(0o755)

    log("smoke", "PASS deterministic candidate")
    return {"status": "passed", "mode": "pipeline_smoke"}
