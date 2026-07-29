import os
import subprocess

import pytest


BRANCH = "auto/new-image/kvrocks/2.16.0-oe2403sp4"


def _config(mode="fork_pr"):
    from scripts.lib.delivery_config import DeliveryConfig

    return DeliveryConfig.from_mapping(
        {
            "environment": "test",
            "delivery_mode": mode,
            "target_repo": "openeuler/openeuler-docker-images",
            "push_repo": "qq_42020325/openeuler-docker-images",
            "target_branch": "master",
        }
    )


class RecordingGitRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, repo, args, env):
        askpass = env["GIT_ASKPASS"]
        self.calls.append(
            {
                "repo": repo,
                "args": list(args),
                "env": dict(env),
                "askpass_exists": os.path.isfile(askpass),
                "askpass_mode": os.stat(askpass).st_mode & 0o777,
                "askpass_content": open(askpass).read(),
            }
        )
        return self.results.pop(0)


def _result(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_push_uses_credential_free_url_askpass_and_empty_branch_lease(tmp_path):
    from scripts.lib.git_delivery import push_working_branch

    token = "token-must-not-enter-command"
    runner = RecordingGitRunner([_result(), _result()])

    push_working_branch(
        repo=tmp_path,
        config=_config(),
        branch=BRANCH,
        username="qq_42020325",
        token=token,
        runner=runner,
    )

    assert len(runner.calls) == 2
    remote = (
        "https://gitcode.com/qq_42020325/openeuler-docker-images.git"
    )
    assert runner.calls[0]["args"] == [
        "ls-remote",
        remote,
        f"refs/heads/{BRANCH}",
    ]
    assert runner.calls[1]["args"] == [
        "push",
        f"--force-with-lease=refs/heads/{BRANCH}:",
        remote,
        f"HEAD:refs/heads/{BRANCH}",
    ]
    for call in runner.calls:
        assert token not in " ".join(call["args"])
        assert call["askpass_exists"] is True
        assert call["askpass_mode"] == 0o700
        assert token not in call["askpass_content"]
        assert "OE_GITCODE_TOKEN" in call["askpass_content"]
        assert call["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert call["env"]["OE_GITCODE_USERNAME"] == "qq_42020325"
        assert call["env"]["OE_GITCODE_TOKEN"] == token


def test_push_uses_exact_observed_sha_as_force_with_lease(tmp_path):
    from scripts.lib.git_delivery import push_working_branch

    old_sha = "1" * 40
    runner = RecordingGitRunner(
        [
            _result(
                stdout=f"{old_sha}\trefs/heads/{BRANCH}\n",
            ),
            _result(),
        ]
    )

    push_working_branch(
        repo=tmp_path,
        config=_config(),
        branch=BRANCH,
        username="qq_42020325",
        token="secret",
        runner=runner,
    )

    assert runner.calls[1]["args"][1] == (
        f"--force-with-lease=refs/heads/{BRANCH}:{old_sha}"
    )


def test_validate_only_and_unsafe_branch_refuse_before_git_runs(tmp_path):
    from scripts.lib.git_delivery import GitDeliveryError, push_working_branch

    runner = RecordingGitRunner([])

    with pytest.raises(GitDeliveryError, match="forbids"):
        push_working_branch(
            repo=tmp_path,
            config=_config("validate_only"),
            branch=BRANCH,
            username="qq_42020325",
            token="secret",
            runner=runner,
        )
    with pytest.raises(GitDeliveryError, match="auto"):
        push_working_branch(
            repo=tmp_path,
            config=_config(),
            branch="master",
            username="qq_42020325",
            token="secret",
            runner=runner,
        )

    assert runner.calls == []


def test_cleanup_deletes_only_exact_observed_auto_branch(tmp_path):
    from scripts.lib.git_delivery import delete_working_branch

    old_sha = "a" * 40
    runner = RecordingGitRunner(
        [
            _result(stdout=f"{old_sha}\trefs/heads/{BRANCH}\n"),
            _result(),
        ]
    )

    deleted = delete_working_branch(
        repo=tmp_path,
        config=_config(),
        branch=BRANCH,
        username="qq_42020325",
        token="secret",
        runner=runner,
    )

    assert deleted is True
    assert runner.calls[1]["args"] == [
        "push",
        f"--force-with-lease=refs/heads/{BRANCH}:{old_sha}",
        "https://gitcode.com/qq_42020325/openeuler-docker-images.git",
        f":refs/heads/{BRANCH}",
    ]


def test_cleanup_is_noop_when_exact_branch_does_not_exist(tmp_path):
    from scripts.lib.git_delivery import delete_working_branch

    runner = RecordingGitRunner([_result(stdout="")])

    deleted = delete_working_branch(
        repo=tmp_path,
        config=_config(),
        branch=BRANCH,
        username="qq_42020325",
        token="secret",
        runner=runner,
    )

    assert deleted is False
    assert len(runner.calls) == 1


def test_git_failure_redacts_token_from_error(tmp_path):
    from scripts.lib.git_delivery import GitDeliveryError, push_working_branch

    token = "never-show-this-token"
    runner = RecordingGitRunner(
        [_result(returncode=1, stderr=f"authentication failed: {token}")]
    )

    with pytest.raises(GitDeliveryError) as error:
        push_working_branch(
            repo=tmp_path,
            config=_config(),
            branch=BRANCH,
            username="qq_42020325",
            token=token,
            runner=runner,
        )

    assert token not in str(error.value)
    assert "REDACTED" in str(error.value)
