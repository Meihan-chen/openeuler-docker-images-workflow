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
        "RUN groupadd -r kvrocks && "
        "useradd -r -g kvrocks kvrocks\n"
        "COPY --from=builder /src/kvrocks/build/kvrocks /usr/local/bin/kvrocks\n"
        "USER kvrocks\n"
        "EXPOSE 6666\n"
        "HEALTHCHECK CMD redis-cli -p 6666 PING | grep PONG\n"
        "ENTRYPOINT [\"kvrocks\", \"--bind\", \"0.0.0.0\"]\n"
    )
    (tests / "test_helpers.sh").write_text(
        "#!/bin/bash\nassert_non_root() { test \"$(id -u)\" != 0; }\n"
    )
    (tests / "test.sh").write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        ": \"${EXPECTED_VERSION:?}\"\n"
        "version_output=\"$(kvrocks --version)\"\n"
        "read -r binary label reported_version details "
        "<<< \"${version_output}\"\n"
        "test \"${binary}\" = kvrocks\n"
        "test \"${label}\" = version\n"
        "test \"${reported_version}\" = \"${EXPECTED_VERSION}\"\n"
        "test -n \"${details}\"\n"
        "ping=\"$(redis-cli -p 6666 PING)\"\n"
        "test \"${ping}\" = PONG\n"
        "test \"$(id -u)\" != 0\n"
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
    assert report["build_allowed"] is True
    assert report["delivery_allowed"] is True
    assert report["test_allowed"] is True
    assert report["findings"] == []
    assert report["task_id"] == _task().task_id
    assert report["added_files"] == 7
    assert report["modified_files"] == ["Database/image-list.yml"]


def test_contract_allows_candidate_without_optional_doc(tmp_path):
    from scripts.harness.gate_diff import validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    shutil.rmtree(repo / "Database" / "kvrocks" / "doc")

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["status"] == "passed"
    assert report["delivery_allowed"] is True
    assert report["findings"] == []


def test_contract_blocks_delivery_for_partially_generated_doc(tmp_path):
    from scripts.harness.gate_diff import validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    (repo / "Database" / "kvrocks" / "doc" / "image-info.yml").unlink()

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["status"] == "passed"
    assert report["build_allowed"] is True
    assert report["delivery_allowed"] is False
    assert report["findings"] == [
        {
            "code": "doc.incomplete",
            "level": "delivery_stop",
            "owner": "image_creator",
            "source": "target_repository_contract",
            "message": "generated doc is missing doc/image-info.yml",
        }
    ]


def test_contract_blocks_delivery_when_generated_doc_has_no_picture(tmp_path):
    from scripts.harness.gate_diff import validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    shutil.rmtree(repo / "Database" / "kvrocks" / "doc" / "picture")

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["build_allowed"] is True
    assert report["delivery_allowed"] is False
    assert any(
        finding["code"] == "doc.picture_required"
        and finding["source"] == "target_repository_contract"
        for finding in report["findings"]
    )


def test_contract_does_not_require_optional_test_helpers(tmp_path):
    from scripts.harness.gate_diff import validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    tests = repo / "Database" / "kvrocks" / "tests"
    (tests / "test_helpers.sh").unlink()

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["test_allowed"] is True
    assert report["delivery_allowed"] is True


def test_test_contract_rejects_invalid_runtime_script(tmp_path):
    from scripts.lib.target_contract import validate_test_contract

    repo, _ = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    (repo / "Database" / "kvrocks" / "tests" / "test.sh").write_text(
        "#!/bin/bash\nif true; then\n"
    )

    report = validate_test_contract(repo=repo, task=_task())

    assert report["runtime_test_allowed"] is False
    assert report["test_allowed"] is False
    assert any(
        finding["code"] == "tests.shell_syntax"
        and finding["check"] == "runtime_test"
        for finding in report["findings"]
    )


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
            )
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
            "USER application\n"
            "CMD [\"sh\"]\n"
        )
    elif variant == "application_selected_test_strategy":
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


@pytest.mark.parametrize(
    "fixed_identity",
    [
        "RUN groupadd --gid 1000 app && useradd -r -g app app\nUSER app\n",
        "RUN groupadd -g 1000 app && useradd -r -g app app\nUSER app\n",
        "RUN groupadd -g1000 app && useradd -r -g app app\nUSER app\n",
        "RUN addgroup -g 1000 app && adduser -S -G app app\nUSER app\n",
        "RUN groupadd -r app && useradd --uid=1000 -g app app\nUSER app\n",
        "RUN groupadd -r app && useradd -u1000 -g app app\nUSER app\n",
        "RUN addgroup -S app && adduser -u 1000 -S -G app app\nUSER app\n",
        "USER 1000:1000\n",
        "RUN mkdir -p /data && chown -R 1000:1000 /data\nUSER app\n",
        "RUN mkdir -p /data && chown -R 1000.1000 /data\nUSER app\n",
        "COPY --chown=1000:1000 binary /usr/local/bin/binary\nUSER app\n",
        "COPY --chown=1000.1000 binary /usr/local/bin/binary\nUSER app\n",
        'RUN groupadd --gid "1000" app && useradd --uid "1000" -g app app\nUSER app\n',
        'RUN ["useradd", "--uid", "1000", "app"]\nUSER app\n',
        "ARG APP_ID=1000\nRUN groupadd -r app && useradd -u ${APP_ID} -g app app\nUSER app\n",
        "ARG APP_ID=1000\nCOPY --chown=${APP_ID}:${APP_ID} binary /bin/binary\nUSER app\n",
        'RUN chown -R "1000:1000" /data\nUSER app\n',
        "RUN install -o 1000 -g 1000 binary /bin/binary\nUSER app\n",
        "RUN install -o1000 -g1000 binary /bin/binary\nUSER app\n",
        "RUN APP_ID=1000; groupadd -g ${APP_ID} app; useradd -u ${APP_ID} -g app app\nUSER app\n",
        'RUN APP_UID=1000 useradd -u "$APP_UID" app\nUSER app\n',
        'RUN env APP_UID=1000 useradd -u "$APP_UID" app\nUSER app\n',
        'RUN /usr/bin/env APP_UID=1000 useradd -u "$APP_UID" app\nUSER app\n',
        'RUN env -i APP_UID=1000 useradd -u "$APP_UID" app\nUSER app\n',
        'RUN APP_ID=1000 chown "$APP_ID:$APP_ID" /app\nUSER app\n',
        "RUN /usr/sbin/useradd -u 1000 app\nUSER app\n",
        "RUN busybox adduser -u 1000 app\nUSER app\n",
        "ARG APP_ID=1000\nUSER ${APP_ID}\n",
    ],
)
def test_contract_rejects_fixed_numeric_identity(tmp_path, fixed_identity):
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
    dockerfile.write_text("FROM openeuler/openeuler:24.03-lts-sp4\n" + fixed_identity)

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["delivery_allowed"] is False
    assert any(
        finding["code"] == "dockerfile.fixed_identity"
        for finding in report["findings"]
    )


@pytest.mark.parametrize(
    "non_command_text",
    [
        "# never use: useradd -u 1000 app\n",
        'RUN echo "never use: useradd -u 1000 app" >/policy\n',
    ],
)
def test_contract_ignores_fixed_identity_text_that_is_not_a_command(
    tmp_path,
    non_command_text,
):
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
        "FROM openeuler/openeuler:24.03-lts-sp4\n"
        + non_command_text
        + "RUN groupadd -r app && useradd -r -g app app\n"
        + "USER app\n"
    )

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["delivery_allowed"] is True


def test_contract_ignores_identity_examples_inside_heredoc(tmp_path):
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
        "FROM openeuler/openeuler:24.03-lts-sp4\n"
        "RUN cat >/policy <<'EOF'\n"
        "useradd -u 1000 app\n"
        "USER 1000\n"
        "EOF\n"
        "USER app\n"
    )

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["delivery_allowed"] is True


@pytest.mark.parametrize(
    "identity_text",
    [
        "CMD [\"sh\"]\n",
        "RUN groupadd -r app\nUSER app\n",
    ],
)
def test_contract_does_not_require_runtime_identity_semantics(
    tmp_path,
    identity_text,
):
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
        "FROM openeuler/openeuler:24.03-lts-sp4\n" + identity_text
    )

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["delivery_allowed"] is True


def test_contract_rejects_change_outside_task_scope(tmp_path):
    from scripts.harness.gate_diff import TargetContractError, validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    (repo / "README.md").write_text("agent changed target rules\n")

    with pytest.raises(TargetContractError, match="outside"):
        validate_generated_target(repo=repo, task=_task(), base_sha=base_sha)


def test_generated_contract_does_not_recheck_existing_app_root(tmp_path):
    from scripts.harness.gate_diff import validate_generated_target

    repo, _ = _repo(tmp_path)
    (repo / "Database" / "kvrocks").mkdir()
    (repo / "Database" / "kvrocks" / "existing.txt").write_text(
        "pre-existing application content\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "existing kvrocks")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _write_valid_generated_candidate(repo)

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["build_allowed"] is True


def test_contract_rejects_image_list_rewrite(tmp_path):
    from scripts.harness.gate_diff import TargetContractError, validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    (repo / "Database" / "image-list.yml").write_text(
        "images:\n  kvrocks: kvrocks\n"
    )

    with pytest.raises(TargetContractError, match="image-list"):
        validate_generated_target(repo=repo, task=_task(), base_sha=base_sha)


def test_contract_defers_single_arch_meta_to_delivery_without_gating_build(tmp_path):
    from scripts.harness.gate_diff import validate_generated_target

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

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["status"] == "passed"
    assert report["build_allowed"] is True
    assert report["delivery_allowed"] is False
    assert any(
        finding["code"] == "meta.dual_arch"
        and "dual-architecture" in finding["message"]
        for finding in report["findings"]
    )


def test_contract_hard_stops_when_meta_cannot_be_parsed(tmp_path):
    from scripts.harness.gate_diff import (
        TargetContractError,
        validate_generated_target,
    )

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    (repo / "Database" / "kvrocks" / "meta.yml").write_text("tag: [\n")

    with pytest.raises(TargetContractError, match="meta.yml.*valid YAML"):
        validate_generated_target(repo=repo, task=_task(), base_sha=base_sha)


def test_contract_classifies_extra_test_asset_as_test_contract(tmp_path):
    from scripts.harness.gate_diff import validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    extra = repo / "Database" / "kvrocks" / "tests" / "readiness.sh"
    extra.write_text("#!/bin/bash\nexit 0\n")

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["build_allowed"] is True
    assert report["test_allowed"] is False
    assert any(
        finding["code"] == "tests.forbidden_control_asset"
        for finding in report["findings"]
    )


@pytest.mark.parametrize("owner", ["openeuler", "src-openeuler"])
def test_contract_blocks_only_open_euler_pre_migration_gitee_links(
    tmp_path,
    owner,
):
    from scripts.harness.gate_diff import validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    readme = repo / "Database" / "kvrocks" / "README.md"
    readme.write_text(
        readme.read_text()
        + f"\nSee https://gitee.com/{owner}/openeuler-docker-images\n"
    )

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["status"] == "passed"
    assert report["delivery_allowed"] is False
    assert any(
        finding["code"] == "links.openeuler_gitee"
        and finding["owner"] == "image_creator"
        for finding in report["findings"]
    )


def test_contract_accepts_third_party_gitee_link_and_plain_host_text(tmp_path):
    from scripts.harness.gate_diff import validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    readme = repo / "Database" / "kvrocks" / "README.md"
    readme.write_text(
        readme.read_text()
        + "\nUpstream: https://gitee.com/example/vendor-project\n"
        + "The word gitee.com alone is not a repository URL.\n"
    )

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["delivery_allowed"] is True
    assert report["findings"] == []


def test_contract_accepts_gitcode_and_upstream_links(tmp_path):
    from scripts.harness.gate_diff import validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    readme = repo / "Database" / "kvrocks" / "README.md"
    readme.write_text(
        readme.read_text()
        + "\n- Maintained by: [openEuler CloudNative SIG]"
        "(https://gitcode.com/openeuler/cloudnative).\n"
        "- Upstream: https://github.com/apache/kvrocks\n"
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


def test_final_contract_accepts_public_evidence_without_a_run_id(tmp_path):
    from scripts.harness.gate_diff import validate_final_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    _write_valid_results(repo)

    report = validate_final_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["status"] == "passed"
    assert "validated_run_id" not in report
    assert report["result_bytes"] <= 20 * 1024


def test_final_contract_rejects_production_results_in_target_repo(tmp_path):
    from scripts.harness.gate_diff import (
        TargetContractError,
        validate_final_target,
    )

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    _write_valid_results(repo)
    result_dir = (
        repo
        / "Database"
        / "kvrocks"
        / "results"
        / "2.16.0"
        / "24.03-lts-sp4"
    )
    (result_dir / "results.json").write_text('{"schema_version": 1}\n')

    with pytest.raises(TargetContractError, match="unexpected results.json"):
        validate_final_target(
            repo=repo,
            task=_task(),
            base_sha=base_sha,
        )


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
        )


@pytest.mark.parametrize(
    "junit",
    (
        '<testsuite tests="0" failures="0" errors="0"/>',
        '<testsuite tests="1" failures="0" errors="0" skipped="1"/>',
    ),
)
def test_final_contract_requires_junit_to_prove_executed_passes(tmp_path, junit):
    from scripts.harness.gate_diff import (
        TargetContractError,
        validate_final_target,
    )

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    _write_valid_results(repo)
    junit_path = (
        repo
        / "Database"
        / "kvrocks"
        / "results"
        / "2.16.0"
        / "24.03-lts-sp4"
        / "aarch64.junit.xml"
    )
    junit_path.write_text(junit)

    with pytest.raises(TargetContractError, match="aarch64 JUnit"):
        validate_final_target(
            repo=repo,
            task=_task(),
            base_sha=base_sha,
        )


def test_native_contract_uses_only_build_and_runtime_test_checks():
    from scripts.lib.target_contract import NATIVE_REQUIRED_CHECKS

    assert NATIVE_REQUIRED_CHECKS == frozenset(
        {"native_build", "runtime_test"}
    )


def test_reserved_control_assets_only_name_current_workflow_controls():
    from scripts.lib.target_contract import _RESERVED_CONTROL_ASSETS

    assert _RESERVED_CONTROL_ASSETS == frozenset(
        {"wait_ready.sh", "mode.yml"}
    )


def test_test_contract_exposes_one_runtime_permission(tmp_path):
    from scripts.lib.target_contract import validate_test_contract

    repo, _ = _repo(tmp_path)
    _write_valid_generated_candidate(repo)

    report = validate_test_contract(repo=repo, task=_task())

    assert report["status"] == "passed"
    assert report["test_allowed"] is True
    assert report["runtime_test_allowed"] is True
    assert set(report) == {
        "status",
        "test_allowed",
        "runtime_test_allowed",
        "findings",
        "errors",
    }


@pytest.mark.parametrize(
    "relative",
    (
        "tests/wait_ready.sh",
        "tests/mode.yml",
        "tests/AGENTS.md",
        "tests/.agents/runtime.md",
    ),
)
def test_contract_rejects_removed_test_control_assets(tmp_path, relative):
    from scripts.lib.target_contract import validate_test_contract

    repo, _ = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    path = repo / "Database" / "kvrocks" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("removed control asset\n")

    report = validate_test_contract(repo=repo, task=_task())

    assert report["status"] == "failed"
    assert report["runtime_test_allowed"] is False
    assert any(
        finding["code"] == "tests.forbidden_control_asset"
        and finding["check"] == "runtime_test"
        for finding in report["findings"]
    )


@pytest.mark.parametrize(
    "relative",
    (
        "2.16.0/24.03-lts-sp4/wait_ready.sh",
        "doc/mode.yml",
        "AGENTS.md",
        ".agents/runtime.md",
    ),
)
def test_image_phase_rejects_reserved_control_assets_anywhere(
    tmp_path,
    relative,
):
    from scripts.lib.target_contract import validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    path = repo / "Database" / "kvrocks" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("reserved workflow control\n")

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
        phase="image",
    )

    assert report["delivery_allowed"] is False
    assert any(
        finding["code"] == "scope.reserved_control_asset"
        and finding["owner"] == "image_creator"
        for finding in report["findings"]
    )


def test_contract_rejects_shared_test_bash_syntax_errors(tmp_path):
    from scripts.lib.target_contract import validate_generated_target

    repo, base_sha = _repo(tmp_path)
    _write_valid_generated_candidate(repo)
    entrypoint = repo / "Database" / "kvrocks" / "tests" / "test.sh"
    entrypoint.write_text(
        "#!/bin/bash\n"
        "if [ -n \"${EXPECTED_VERSION}\" ]; then\n"
        "    kvrocks --version\n"
    )

    report = validate_generated_target(
        repo=repo,
        task=_task(),
        base_sha=base_sha,
    )

    assert report["build_allowed"] is True
    assert report["test_allowed"] is False
    assert any(
        item["code"] == "tests.shell_syntax"
        and item["owner"] == "testcase_creator"
        for item in report["findings"]
    )
