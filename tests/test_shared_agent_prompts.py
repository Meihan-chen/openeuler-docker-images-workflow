from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = ROOT / ".github" / "agents"


def test_scenario_one_reuses_the_shared_agent_definitions():
    from scripts.lib.generation_pipeline import build_role_prompt
    from scripts.lib.task_spec import TaskSpec

    assert not (AGENTS_DIR / "phase1").exists()

    task = TaskSpec.from_workflow_dispatch(
        {
            "app": "kvrocks",
            "version": "2.16.0",
            "os_version": "24.03-lts-sp4",
            "domain": "Database",
            "source_url": "https://github.com/apache/kvrocks/tree/v2.16.0",
        }
    )
    prompt = build_role_prompt(
        role="image_creator",
        task=task,
        base_sha="1" * 40,
    )

    assert (AGENTS_DIR / "image-creator.md").read_text().strip() in prompt
    assert "Immutable task contract" in prompt


def test_shared_qa_prompts_preserve_findings_for_local_validation():
    image_qa = (AGENTS_DIR / "image-qa.md").read_text().lower()
    testcase_qa = (AGENTS_DIR / "testcase-qa.md").read_text().lower()
    testcase_creator = (AGENTS_DIR / "testcase-creator.md").read_text().lower()

    assert "approve anyway" not in image_qa
    assert "must fail closed" not in image_qa
    assert "must fail closed" not in testcase_qa
    assert "local validation" in image_qa
    assert "local validation" in testcase_qa
    assert "模糊匹配" not in testcase_creator
    assert "exact" in testcase_creator or "精确" in testcase_creator
    assert "timeout: 30000" not in testcase_creator
    assert "ss -tlnp" not in testcase_creator
    assert "runtime image" in testcase_creator
    assert "runtime image" in testcase_qa
    assert "test-ai-result.json" not in testcase_creator
    assert "container_name" not in testcase_creator
    assert "与 dockerfile 同级" not in testcase_creator
    assert "dockerfile-level" not in testcase_qa


def test_image_prompts_define_generic_auxiliary_file_policy():
    image_creator = (AGENTS_DIR / "image-creator.md").read_text().lower()
    image_qa = (AGENTS_DIR / "image-qa.md").read_text().lower()

    assert "minimum required structure" in image_creator
    assert "doc/ is optional" in image_creator
    assert "if any doc/ content is created" in image_creator
    assert "at least one doc/picture asset" in image_creator
    assert "at least one doc/picture asset" in image_qa
    assert "upstream-provided configuration" in image_creator
    assert "persistent data directory" in image_creator
    assert "auxiliary files" in image_qa
    assert "configuration provenance" in image_qa


def test_image_prompts_keep_source_fetches_reproducible_and_bounded():
    image_creator = " ".join(
        (AGENTS_DIR / "image-creator.md").read_text().lower().split()
    )
    image_qa = " ".join(
        (AGENTS_DIR / "image-qa.md").read_text().lower().split()
    )

    for prompt in (image_creator, image_qa):
        assert "taskspec source origin" in prompt
        assert "immutable" in prompt
        assert "bounded retry" in prompt
        assert "published checksum" in prompt
        assert "unverified mirror fallback" in prompt


def test_image_prompts_reuse_unchanged_upstream_assets_from_builder():
    image_creator = " ".join(
        (AGENTS_DIR / "image-creator.md").read_text().lower().split()
    )
    image_qa = " ".join(
        (AGENTS_DIR / "image-qa.md").read_text().lower().split()
    )

    assert "固定版本的 builder 源码" in image_creator
    assert "未修改的上游附属文件" in image_creator
    assert "逐字节相同" in image_creator
    assert "必要的本地定制" in image_creator
    assert "复用策略" in image_creator
    assert "stage alias" in image_creator
    assert "copy --from" in image_creator

    assert "unchanged upstream asset" in image_qa
    assert "copy --from" in image_qa
    assert "byte-identical" in image_qa
    assert "necessary local customization" in image_qa


def test_image_prompts_restrict_only_open_euler_owned_gitee_links():
    image_creator = (AGENTS_DIR / "image-creator.md").read_text().lower()
    image_qa = (AGENTS_DIR / "image-qa.md").read_text().lower()

    for prompt in (image_creator, image_qa):
        assert "third-party gitee" in prompt
        assert "gitee.com/openeuler" in prompt
        assert "gitee.com/src-openeuler" in prompt
    assert "`gitee.com` 是 openeuler 迁移前的旧域名，一律禁止" not in image_creator


def test_testcase_prompts_make_wait_and_helper_files_conditional():
    testcase_creator = (AGENTS_DIR / "testcase-creator.md").read_text().lower()
    testcase_qa = (AGENTS_DIR / "testcase-qa.md").read_text().lower()

    for prompt in (testcase_creator, testcase_qa):
        assert "goss_wait.yaml is optional" in prompt
        assert "test_helpers.sh is optional" in prompt


def test_testcase_prompts_define_service_and_cli_native_modes():
    testcase_creator = " ".join(
        (AGENTS_DIR / "testcase-creator.md").read_text().lower().split()
    )
    testcase_qa = " ".join(
        (AGENTS_DIR / "testcase-qa.md").read_text().lower().split()
    )

    for prompt in (testcase_creator, testcase_qa):
        assert "absence declares cli/one-shot mode" in prompt
        assert "bounded readiness" in prompt
    assert "read back after restart by the harness" not in testcase_qa


def test_shared_prompts_derive_application_behavior_instead_of_assuming_it():
    image_creator = (AGENTS_DIR / "image-creator.md").read_text().lower()
    image_qa = (AGENTS_DIR / "image-qa.md").read_text().lower()
    testcase_creator = (AGENTS_DIR / "testcase-creator.md").read_text().lower()
    testcase_qa = (AGENTS_DIR / "testcase-qa.md").read_text().lower()
    prompts = "\n".join(
        (image_creator, image_qa, testcase_creator, testcase_qa)
    )

    assert "official upstream" in image_creator
    assert "if the application or task requires" in image_qa
    assert "dockerfile 与 official upstream" in " ".join(
        testcase_creator.split()
    )
    assert "if the image requires" in testcase_qa
    for fragment in ("uid 999", "tcp 6666", "redis-cli", "./x.py build"):
        assert fragment not in prompts


def test_image_prompts_treat_fixed_numeric_identity_as_a_collision_risk():
    """Fixed identities need upstream evidence and native collision checks."""
    image_creator = " ".join(
        (AGENTS_DIR / "image-creator.md").read_text().lower().split()
    )
    image_qa = " ".join(
        (AGENTS_DIR / "image-qa.md").read_text().lower().split()
    )

    # The default is an identity that cannot collide.
    assert "groupadd -r" in image_creator
    assert "useradd -r" in image_creator
    assert '"evidence"' in image_creator
    assert '"excerpts"' in image_creator
    assert "creator 不得自行声明" in image_creator
    assert "原生构建" in image_creator

    assert "fixed number" in image_qa
    assert "harness-fixed" in image_qa
    assert "creator cannot prove" in image_qa
    assert "native build is authoritative" in image_qa


def test_image_qa_leaves_unproven_package_resolution_to_native_build():
    image_qa = " ".join(
        (AGENTS_DIR / "image-qa.md").read_text().lower().split()
    )

    assert "concrete evidence in the provided snapshot" in image_qa
    assert "native build is authoritative" in image_qa


def test_image_prompts_do_not_require_one_dockerfile_spelling_or_package_blacklist():
    image_creator = (AGENTS_DIR / "image-creator.md").read_text().lower()
    image_qa = (AGENTS_DIR / "image-qa.md").read_text().lower()

    assert "禁止使用的包" not in image_creator
    assert "arg version 全大写" not in image_creator
    assert "arg base=openeuler/openeuler" not in image_qa
    assert "version_filter 完整" not in image_creator


def test_testcase_prompts_keep_goss_order_independent_and_tests_in_sync():
    testcase_creator = (AGENTS_DIR / "testcase-creator.md").read_text().lower()
    testcase_qa = (AGENTS_DIR / "testcase-qa.md").read_text().lower()

    for prompt in (testcase_creator, testcase_qa):
        normalized = " ".join(prompt.split())
        assert "order-independent" in normalized
        assert "final dockerfile" in normalized
        assert "test.sh" in normalized
    assert "stateful sequence" in " ".join(testcase_creator.split())
    assert "cross-resource ordering" in " ".join(testcase_qa.split())


def test_testcase_guidance_avoids_scalar_goss_port_ip_assertions():
    testcase_creator = (AGENTS_DIR / "testcase-creator.md").read_text().lower()
    testcase_qa = (AGENTS_DIR / "testcase-qa.md").read_text().lower()
    goss_template = (ROOT / "templates" / "test" / "goss.yaml.j2").read_text()

    for content in (testcase_creator, goss_template.lower()):
        assert 'ip: "0.0.0.0"' not in content

    # The Creator states the general criterion — confirm the value type before
    # asserting on it — with `port.*.ip` as the worked example.
    creator = " ".join(testcase_creator.split())
    assert "port.*.ip" in creator
    assert "标量" in creator
    assert "集合" in creator
    assert "listening: true" in creator

    reviewer = " ".join(testcase_qa.split())
    assert "multiple listening sockets" in reviewer
    assert "scalar" in reviewer
    assert "functional" in reviewer


def test_fixer_prompt_synchronizes_observable_runtime_contract_consumers():
    fixer = (AGENTS_DIR / "code-fixer.md").read_text().lower()

    assert "observable runtime contract" in fixer
    assert "dependent candidate files" in fixer
    assert "re-read" in fixer
    assert "must not weaken" in fixer


def test_fixer_prompt_documents_the_payload_the_harness_actually_sends():
    """The declared inputs described a payload that never existed.

    build_logs, test_output, fix_branch and knowledge_base were never sent, and
    the prompt's own taxonomy used names the classifier does not produce, so
    the Agent was reading a schema for a different harness.
    """
    from scripts.lib.failure_classification import _GUIDANCE

    fixer = (AGENTS_DIR / "code-fixer.md").read_text()

    for absent in ("build_logs", "test_output", "fix_branch", "knowledge_base"):
        assert absent not in fixer
    for field in (
        "classification",
        "repair_round",
        "architectures",
        "failed_stage",
        "failure_details",
        "container_evidence",
        "stdout_head",
    ):
        assert field in fixer
    for category in _GUIDANCE:
        assert category in fixer


def test_testcase_prompts_require_command_semantics_evidence():
    """Same command name does not mean same behavior.

    Run 30781977554 asserted Kvrocks `DBSIZE` with Redis semantics. Both
    prompts described deriving behavior from upstream, but neither asked for
    the derivation, so nothing could be reviewed.
    """
    creator = " ".join((AGENTS_DIR / "testcase-creator.md").read_text().split())
    reviewer = " ".join((AGENTS_DIR / "testcase-qa.md").read_text().split())

    assert "command_evidence" in creator
    assert '"semantics"' in creator
    assert '"evidence_id"' in creator
    assert '"evidence"' in creator
    assert '"excerpts"' in creator

    assert "command_evidence" in reviewer
    assert "Harness-fixed" in reviewer
    assert "not a terminal veto" in reviewer
    assert "asynchronous cache" in reviewer


def test_testcase_qa_issues_must_cite_evidence():
    reviewer = " ".join((AGENTS_DIR / "testcase-qa.md").read_text().split())

    assert '"evidence"' in reviewer
    assert "does not count as a blocker or major" in reviewer


def test_creator_prompts_keep_evidence_within_harness_bounds():
    for name in ("image-creator.md", "testcase-creator.md"):
        prompt = " ".join((AGENTS_DIR / name).read_text().split())
        assert "最多 6" in prompt
        assert "1—2" in prompt
        assert "逐字" in prompt
        assert "TaskSpec 同源" in prompt
        assert "固定 revision" in prompt


def test_creator_provides_evidence_and_qa_reviews_it_without_a_gate():
    creators = "\n".join(
        (AGENTS_DIR / name).read_text()
        for name in ("image-creator.md", "testcase-creator.md")
    )
    reviewers = "\n".join(
        (AGENTS_DIR / name).read_text()
        for name in ("image-qa.md", "testcase-qa.md")
    )
    normalized_reviewers = " ".join(reviewers.split())

    assert '"excerpts"' in creators
    assert "locator" not in creators
    assert "evidence_reviews" in normalized_reviewers
    assert "must not trigger" in normalized_reviewers
    assert "original review" in normalized_reviewers
    assert "Record one actionable concern for every missing command" not in reviewers


def test_fixer_does_not_repair_evidence_metadata():
    fixer = " ".join((AGENTS_DIR / "code-fixer.md").read_text().split())

    assert "证据元数据" in fixer
    assert "不得为了让证据审核通过" in fixer


def test_testcase_prompts_only_defer_shell_syntax_to_the_generation_gate():
    """Goss schema is exercised by native dgoss, not a generation sandbox."""
    creator = " ".join((AGENTS_DIR / "testcase-creator.md").read_text().split())
    reviewer = " ".join((AGENTS_DIR / "testcase-qa.md").read_text().split())

    assert "tests.goss_render" not in creator
    assert "tests.shell_syntax" in creator
    assert "tests.goss_render" not in reviewer
    assert "pinned Goss" in reviewer
