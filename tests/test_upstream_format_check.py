import subprocess
import platform


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _commit_repo(path, files):
    path.mkdir()
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=master", str(path)],
        check=True,
    )
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _git(path, "add", "--", ".")
    _git(path, "commit", "-qm", "fixture")
    return path


def test_sparse_cloned_format_checker_really_runs_on_aarch64(tmp_path):
    from scripts.lib.upstream_format_check import run_upstream_format_check

    upstream = _commit_repo(
        tmp_path / "upstream",
        {
            "update/container/app/format.py": (
                "import platform\n"
                "def check_report(change_files):\n"
                "    if platform.machine() != 'x86_64':\n"
                "        return '', '', 0\n"
                "    if 'README.md' in change_files:\n"
                "        return '<tr></tr>', '<tr>README.md format failed</tr>', 1\n"
                "    return '<tr></tr>', '', 0\n"
            )
        },
    )
    workspace = _commit_repo(
        tmp_path / "target",
        {"README.md": "before\n"},
    )
    (workspace / "README.md").write_text("after\n")

    result = run_upstream_format_check(
        workspace=workspace,
        architecture="aarch64",
        temp_root=tmp_path / "runtime",
        repository=str(upstream),
    )

    assert result["status"] == "failed"
    assert result["kind"] == "candidate"
    assert result["runner_architecture"] == "aarch64"
    assert result["compatibility_override"] is True
    assert result["changed_files"] == ["README.md"]
    assert result["fail_count"] == 1
    assert "README.md format failed" in result["output"]
    assert len(result["commit_sha"]) == 40


def test_clone_failure_is_returned_as_infrastructure_evidence(tmp_path):
    from scripts.lib.upstream_format_check import run_upstream_format_check

    workspace = _commit_repo(
        tmp_path / "target",
        {"README.md": "before\n"},
    )
    (workspace / "README.md").write_text("after\n")

    result = run_upstream_format_check(
        workspace=workspace,
        architecture="x86_64",
        temp_root=tmp_path / "runtime",
        repository=str(tmp_path / "missing-upstream"),
    )

    assert result["status"] == "failed"
    assert result["kind"] == "infra"
    assert result["stage"] == "clone"
    assert result["runner_architecture"] == "x86_64"
    assert result["failure"]


def test_clone_always_reads_master_even_when_repository_head_moves(tmp_path):
    from scripts.lib.upstream_format_check import run_upstream_format_check

    upstream = _commit_repo(
        tmp_path / "upstream",
        {
            "update/container/app/format.py": (
                "def check_report(change_files):\n"
                "    return '', 'checker from master', 0\n"
            )
        },
    )
    master_sha = _git(upstream, "rev-parse", "HEAD").stdout.strip()
    _git(upstream, "checkout", "-qb", "future-default")
    source = upstream / "update/container/app/format.py"
    source.write_text(
        "def check_report(change_files):\n"
        "    return '', 'checker from another branch', 0\n"
    )
    _git(upstream, "add", "--", source.relative_to(upstream))
    _git(upstream, "commit", "-qm", "future branch")
    workspace = _commit_repo(
        tmp_path / "target",
        {"README.md": "before\n"},
    )
    (workspace / "README.md").write_text("after\n")

    result = run_upstream_format_check(
        workspace=workspace,
        architecture="x86_64",
        temp_root=tmp_path / "runtime",
        repository=str(upstream),
    )

    assert result["status"] == "passed"
    assert result["commit_sha"] == master_sha
    assert "checker from master" in result["output"]


def test_declared_x86_runner_executes_check_even_on_a_non_x86_test_host(
    tmp_path,
    monkeypatch,
):
    from scripts.lib.upstream_format_check import run_upstream_format_check

    upstream = _commit_repo(
        tmp_path / "upstream",
        {
            "update/container/app/format.py": (
                "import platform\n"
                "def check_report(change_files):\n"
                "    if platform.machine() != 'x86_64':\n"
                "        return '', 'silently skipped', 0\n"
                "    return '', 'x86 check executed', 1\n"
            )
        },
    )
    workspace = _commit_repo(
        tmp_path / "target",
        {"README.md": "before\n"},
    )
    (workspace / "README.md").write_text("after\n")
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")

    result = run_upstream_format_check(
        workspace=workspace,
        architecture="x86_64",
        temp_root=tmp_path / "runtime",
        repository=str(upstream),
    )

    assert result["status"] == "failed"
    assert result["compatibility_override"] is False
    assert "x86 check executed" in result["output"]
