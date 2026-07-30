import subprocess
import json
import shutil
import xml.etree.ElementTree as ET

import pytest


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _task():
    from scripts.lib.task_spec import TaskSpec

    return TaskSpec.from_workflow_dispatch(
        {
            "app": "kvrocks",
            "version": "2.16.0",
            "os_version": "24.03-lts-sp4",
            "domain": "Database",
            "source_url": "https://github.com/apache/kvrocks/tree/v2.16.0",
        }
    )


def _repo(tmp_path):
    repo = tmp_path / "target"
    subprocess.run(
        ["git", "init", "-b", "master", str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.com")
    (repo / "Database").mkdir()
    (repo / "Database" / "image-list.yml").write_text(
        "images:\n  redis: redis\n"
    )
    (repo / "README.md").write_text("target rules\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo, _git(repo, "rev-parse", "HEAD")


def _write_valid_generated_candidate(repo):
    app = repo / "Database" / "kvrocks"
    image = app / "2.16.0" / "24.03-lts-sp4"
    tests = app / "tests"
    doc = app / "doc"
    (doc / "picture").mkdir(parents=True)
    image.mkdir(parents=True)
    tests.mkdir()

    (repo / "Database" / "image-list.yml").write_text(
        "images:\n  redis: redis\n  kvrocks: kvrocks\n"
    )
    (app / "meta.yml").write_text(
        "2.16.0-oe2403sp4:\n"
        "  path: 2.16.0/24.03-lts-sp4/Dockerfile\n"
    )
    (app / "README.md").write_text(
        "# Quick reference\n\n"
        "# Apache Kvrocks | openEuler\n\n"
        "# Supported tags and respective Dockerfile links\n\n"
        "2.16.0-oe2403sp4\n\n"
        "# Usage\n\n"
        "docker run openeuler/kvrocks:2.16.0-oe2403sp4\n\n"
        "# Question and answering\n"
    )
    (doc / "image-info.yml").write_text(
        "name: kvrocks\n"
        "category: database\n"
        "description: Apache Kvrocks is a distributed key-value database.\n"
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
    (doc / "picture" / "logo.png").write_bytes(
        b"\x89PNG\r\n\x1a\nfixture"
    )
    (image / "Dockerfile").write_text(
        "ARG BASE=openeuler/openeuler:24.03-lts-sp4\n"
        "FROM ${BASE} AS builder\n"
        "ARG VERSION=2.16.0\n"
        "RUN curl -fSL -o source.tar.gz "
        "https://github.com/apache/kvrocks/archive/refs/tags/"
        "v${VERSION}.tar.gz && mkdir -p /src/kvrocks && "
        "tar -zxf source.tar.gz -C /src/kvrocks --strip-components=1 && "
        "cd /src/kvrocks && ./x.py build -j 4\n"
        "FROM ${BASE}\n"
        "RUN groupadd --gid 999 kvrocks && "
        "useradd --uid 999 --gid kvrocks kvrocks\n"
        "COPY --from=builder /src/kvrocks/build/kvrocks /usr/local/bin/kvrocks\n"
        "USER 999\n"
        "EXPOSE 6666\n"
        "HEALTHCHECK CMD redis-cli -p 6666 PING | grep PONG\n"
        "ENTRYPOINT [\"kvrocks\", \"--bind\", \"0.0.0.0\"]\n"
    )
    (tests / "goss.yaml").write_text(
        "port:\n"
        "  tcp:6666:\n"
        "    listening: true\n"
        "command:\n"
        "  version:\n"
        "    exec: kvrocks --version\n"
        "    stdout:\n"
        "      - \"{{.Env.EXPECTED_VERSION}}\"\n"
        "  ping:\n"
        "    exec: redis-cli -p 6666 PING\n"
        "    stdout:\n"
        "      - PONG\n"
    )
    (tests / "goss_wait.yaml").write_text(
        "port:\n  tcp:6666:\n    listening: true\n"
    )
    (tests / "test_helpers.sh").write_text(
        "#!/bin/bash\nwait_for_kvrocks() { redis-cli -p 6666 PING; }\n"
    )
    (tests / "test.sh").write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        ": \"${EXPECTED_VERSION:?}\"\n"
        "kvrocks --version | grep -F \"${EXPECTED_VERSION}\"\n"
        "redis-cli -p 6666 PING | grep -F PONG\n"
        "test \"$(id -u)\" = 999\n"
    )
    (tests / "test.sh").chmod(0o755)


def _write_valid_results(repo):
    root = (
        repo
        / "Database"
        / "kvrocks"
        / "results"
        / "2.16.0"
        / "24.03-lts-sp4"
    )
    root.mkdir(parents=True)
    for architecture in ("x86_64", "aarch64"):
        suite = ET.Element(
            "testsuite",
            {"tests": "1", "failures": "0", "errors": "0"},
        )
        ET.SubElement(suite, "testcase", {"name": architecture})
        ET.ElementTree(suite).write(
            root / f"{architecture}.junit.xml",
            encoding="utf-8",
            xml_declaration=True,
        )
    version_info = {
        "test_time": "2026-07-28T12:00:00Z",
        "Model": "two native runners",
        "architecture": "x86_64,aarch64",
        "kernel": "per-architecture evidence",
        "os": "openEuler 24.03 LTS-SP4",
        "cpu_model": "per-architecture evidence",
        "cpu_cores": 16,
        "software_name": "kvrocks",
        "software_version": "2.16.0",
        "python_version": "3.11.6",
        "numpy_version": "not-installed",
    }
    (root / "version_info.json").write_text(json.dumps(version_info))
    (root / "results.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "passed",
                "task_id": _task().task_id,
                "validated_run_id": "123456",
                "artifact_url": (
                    "https://github.com/Meihan-chen/repo/actions/runs/123456"
                ),
                "architectures": {
                    "x86_64": {"checks": {"all": True}},
                    "aarch64": {"checks": {"all": True}},
                },
            }
        )
    )


def _remove_testcase_owned_files(repo):
    app = repo / "Database" / "kvrocks"
    shutil.rmtree(app / "tests")


def test_image_phase_accepts_candidate_before_testcase_creator(tmp_path):
    from scripts.harness.gate_diff import validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    _remove_testcase_owned_files(repo)

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
        phase="image",
    )

    assert report["status"] == "passed"
    assert report["phase"] == "image"


def test_valid_generated_kvrocks_candidate_passes_contract(tmp_path):
    from scripts.harness.gate_diff import validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["status"] == "passed"
    assert report["task_id"] == _task().task_id
    assert report["added_files"] == 9
    assert report["modified_files"] == ["Database/image-list.yml"]


def test_generated_contract_uses_only_the_app_shared_test_entrypoint(tmp_path):
    from scripts.harness.gate_diff import validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["status"] == "passed"


@pytest.mark.parametrize(
    "variant",
    [
        "unbraced_base_variable",
        "alternative_builder_alias",
        "named_runtime_user",
        "numeric_runtime_user_with_group",
        "git_clone_source",
        "fixed_binary_variable",
        "defaulted_binary_variable",
        "application_selected_build_and_runtime",
        "application_selected_test_strategy",
    ],
)
def test_generated_contract_does_not_gate_application_implementation_syntax(
    tmp_path,
    variant,
):
    from scripts.harness.gate_diff import validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    app = repo / "Database" / "kvrocks"
    dockerfile = app / "2.16.0" / "24.03-lts-sp4" / "Dockerfile"
    dockerfile_text = dockerfile.read_text()
    if variant == "unbraced_base_variable":
        dockerfile.write_text(
            dockerfile_text.replace("FROM ${BASE}", "FROM $BASE")
        )
    elif variant == "alternative_builder_alias":
        dockerfile.write_text(
            dockerfile_text
            .replace("AS builder", "AS build")
            .replace("--from=builder", "--from=build")
        )
    elif variant == "named_runtime_user":
        dockerfile.write_text(
            dockerfile_text.replace(
                "FROM ${BASE}\nRUN groupadd",
                "FROM openeuler/openeuler:24.03-lts-sp4\nRUN groupadd",
            ).replace("USER 999", "USER kvrocks")
        )
    elif variant == "numeric_runtime_user_with_group":
        dockerfile.write_text(
            dockerfile_text.replace("USER 999", "USER 999:999")
        )
    elif variant == "git_clone_source":
        dockerfile.write_text(
            dockerfile_text.replace(
                "RUN curl -fSL -o source.tar.gz "
                "https://github.com/apache/kvrocks/archive/refs/tags/"
                "v${VERSION}.tar.gz && mkdir -p /src/kvrocks && "
                "tar -zxf source.tar.gz -C /src/kvrocks --strip-components=1 && "
                "cd /src/kvrocks && ./x.py build -j 4",
                "RUN git clone --depth 1 -b v${VERSION} "
                "https://github.com/apache/kvrocks.git /src/kvrocks && "
                "cd /src/kvrocks && ./x.py build -j 4",
            )
        )
    elif variant in {
        "fixed_binary_variable",
        "defaulted_binary_variable",
    }:
        shared_test = app / "tests" / "test.sh"
        assignment = (
            'BINARY=kvrocks\n"$BINARY" --version'
            if variant == "fixed_binary_variable"
            else 'BINARY="${BINARY:-kvrocks}"\n"${BINARY}" --version'
        )
        shared_test.write_text(
            shared_test.read_text().replace(
                "kvrocks --version",
                assignment,
            )
        )
    elif variant == "application_selected_build_and_runtime":
        dockerfile.write_text(
            "ARG BASE=\"openeuler/openeuler:24.03-lts-sp4\"\n"
            "FROM ${BASE}\n"
            "ARG VERSION=\"2.16.0\"\n"
            "RUN printf '%s\\n' \"${VERSION}\" >/requested-version\n"
            "USER 1000:1000\n"
            "CMD [\"sh\"]\n"
        )
    elif variant == "application_selected_test_strategy":
        (app / "tests" / "goss.yaml").write_text(
            "command:\n"
            "  application-check:\n"
            "    exec: /usr/local/bin/application check\n"
            "    exit-status: 0\n"
        )
        (app / "tests" / "goss_wait.yaml").write_text(
            "command:\n"
            "  application-ready:\n"
            "    exec: /usr/local/bin/application ready\n"
            "    exit-status: 0\n"
        )
        (app / "tests" / "test.sh").write_text(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "/usr/local/bin/application check\n"
        )
    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["status"] == "passed"


def test_contract_does_not_require_one_application_runtime_uid(tmp_path):
    from scripts.harness.gate_diff import validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    dockerfile = (
        repo
        / "Database"
        / "kvrocks"
        / "2.16.0"
        / "24.03-lts-sp4"
        / "Dockerfile"
    )
    dockerfile.write_text(
        dockerfile.read_text()
        .replace("--uid 999", "--uid 1000")
        .replace("USER 999", "USER kvrocks")
    )

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["status"] == "passed"


def test_contract_rejects_change_outside_task_scope(tmp_path):
    from scripts.harness.gate_diff import TargetContractError, validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    (repo / "README.md").write_text("agent changed target rules\n")

    with pytest.raises(TargetContractError, match="outside"):
        validate_generated_target(repo=repo, task=_task(), base_sha=base_sha)


def test_contract_rejects_existing_app_in_base(tmp_path):
    from scripts.harness.gate_diff import TargetContractError, validate_generated_target

    repo, _ = _repo(tmp_path)
    (repo / "Database" / "kvrocks").mkdir()
    (repo / "Database" / "kvrocks" / "README.md").write_text("already exists\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "existing kvrocks")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _write_valid_generated_candidate(repo)

    with pytest.raises(TargetContractError, match="already exists"):
        validate_generated_target(repo=repo, task=_task(), base_sha=base_sha)


def test_contract_rejects_image_list_rewrite(tmp_path):
    from scripts.harness.gate_diff import TargetContractError, validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    (repo / "Database" / "image-list.yml").write_text(
        "images:\n  kvrocks: kvrocks\n"
    )

    with pytest.raises(TargetContractError, match="image-list"):
        validate_generated_target(repo=repo, task=_task(), base_sha=base_sha)


def test_contract_rejects_single_arch_meta_without_gating_build_command(tmp_path):
    from scripts.harness.gate_diff import TargetContractError, validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    app = repo / "Database" / "kvrocks"
    (app / "meta.yml").write_text(
        "2.16.0-oe2403sp4:\n"
        "  path: 2.16.0/24.03-lts-sp4/Dockerfile\n"
        "  arch: aarch64\n"
    )
    dockerfile = app / "2.16.0" / "24.03-lts-sp4" / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text().replace("-j 4", "-j $(nproc)"))

    with pytest.raises(TargetContractError) as error:
        validate_generated_target(repo=repo, task=_task(), base_sha=base_sha)

    message = str(error.value)
    assert "dual-architecture" in message
    assert "-j 4" not in message


def test_contract_rejects_non_list_goss_stdout_matcher(tmp_path):
    from scripts.harness.gate_diff import (
        TargetContractError,
        validate_generated_target,
    )

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    goss = repo / "Database" / "kvrocks" / "tests" / "goss.yaml"
    goss.write_text(
        "port:\n"
        "  tcp:6666:\n"
        "    listening: true\n"
        "command:\n"
        "  version:\n"
        "    exec: kvrocks --version\n"
        "    stdout:\n"
        "      contains: \"{{.Env.EXPECTED_VERSION}}\"\n"
        "  ping:\n"
        "    exec: redis-cli -p 6666 PING\n"
        "    stdout:\n"
        "      contains: PONG\n"
    )

    with pytest.raises(TargetContractError, match="stdout.*YAML list"):
        validate_generated_target(
            repo=repo,
            task=_task(),
            base_sha=base_sha,
        )


def test_contract_rejects_command_timeout_on_goss_port_resource(tmp_path):
    from scripts.harness.gate_diff import (
        TargetContractError,
        validate_generated_target,
    )

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    goss_wait = (
        repo / "Database" / "kvrocks" / "tests" / "goss_wait.yaml"
    )
    goss_wait.write_text(
        "port:\n"
        "  tcp:6666:\n"
        "    listening: true\n"
        "    timeout: 30000\n"
    )

    with pytest.raises(
        TargetContractError,
        match="goss_wait.yaml.*timeout.*retry",
    ):
        validate_generated_target(
            repo=repo,
            task=_task(),
            base_sha=base_sha,
        )


def test_contract_accepts_application_selected_goss_wait_resource(tmp_path):
    from scripts.harness.gate_diff import validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    goss_wait = (
        repo / "Database" / "kvrocks" / "tests" / "goss_wait.yaml"
    )
    goss_wait.write_text(
        "command:\n"
        "  redis-cli -p 6666 PING:\n"
        "    exit-status: 0\n"
    )

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["status"] == "passed"


def test_contract_derives_documented_tag_from_task_os_version(tmp_path):
    from scripts.harness.gate_diff import validate_generated_target
    from scripts.lib.task_spec import TaskSpec

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    app = repo / "Database" / "kvrocks"
    old_image = app / "2.16.0" / "24.03-lts-sp4"
    new_image = app / "2.16.0" / "24.03-lts-sp2"
    old_image.rename(new_image)
    task = TaskSpec.from_workflow_dispatch(
        {
            "app": "kvrocks",
            "version": "2.16.0",
            "os_version": "24.03-lts-sp2",
            "domain": "Database",
            "source_url": "https://github.com/apache/kvrocks/tree/v2.16.0",
        }
    )
    (app / "meta.yml").write_text(
        "2.16.0-oe2403sp2:\n"
        "  path: 2.16.0/24.03-lts-sp2/Dockerfile\n"
    )
    for relative in ("README.md", "doc/image-info.yml"):
        path = app / relative
        path.write_text(path.read_text().replace("oe2403sp4", "oe2403sp2"))
    dockerfile = new_image / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text().replace("24.03-lts-sp4", "24.03-lts-sp2")
    )

    report = validate_generated_target(
        repo=repo,
        task=task,
        base_sha=base_sha,
    )

    assert report["status"] == "passed"


def test_final_contract_requires_and_accepts_bounded_dual_arch_results(tmp_path):
    from scripts.harness.gate_diff import validate_final_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    _write_valid_results(repo)

    report = validate_final_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
        expected_run_id="123456",
    )

    assert report["status"] == "passed"
    assert report["validated_run_id"] == "123456"
    assert report["result_bytes"] <= 20 * 1024


def test_final_contract_rejects_missing_architecture_junit(tmp_path):
    from scripts.harness.gate_diff import (
        TargetContractError,
        validate_final_target,
    )

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    _write_valid_results(repo)
    (
        repo
        / "Database"
        / "kvrocks"
        / "results"
        / "2.16.0"
        / "24.03-lts-sp4"
        / "aarch64.junit.xml"
    ).unlink()

    with pytest.raises(TargetContractError, match="aarch64"):
        validate_final_target(
            repo=repo,
            task=_task(),
            base_sha=base_sha,
            expected_run_id="123456",
        )
