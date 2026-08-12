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
    testcase_qa = (AGENTS_DIR / "testcase-qa.md").read_text().lower()
    testcase_creator = (AGENTS_DIR / "testcase-creator.md").read_text().lower()

    assert "must fail closed" not in testcase_qa
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


def test_image_creator_defines_generic_auxiliary_file_policy():
    image_creator = (AGENTS_DIR / "image-creator.md").read_text().lower()

    assert "最小必需结构" in image_creator
    assert "`doc/` 是可选目录" in image_creator
    assert "只要生成了任何 `doc/` 内容" in image_creator
    assert "至少有一个图片资源" in image_creator
    assert "上游提供的那份" in image_creator
    assert "持久化数据目录" in image_creator


def test_image_creator_keeps_source_fetches_reproducible_and_bounded():
    image_creator = " ".join(
        (AGENTS_DIR / "image-creator.md").read_text().lower().split()
    )
    assert "source_repo_url" in image_creator
    assert "不可变" in image_creator
    assert "有限次数" in image_creator
    assert "checksum" in image_creator
    assert "未经校验的镜像站" in image_creator


def test_image_creator_bounds_optional_release_artifact_research():
    """Run 30872642022 spent 25 of its 30 minutes on avoidable downloads.

    The rule has to be the cost principle plus the cheaper route that answers
    the same question. A list of artifact names taken from one log would miss
    whatever the next application downloads instead, and an archive-format
    recipe would be wrong for the formats it does not cover.
    """
    image_creator = " ".join(
        (AGENTS_DIR / "image-creator.md").read_text().lower().split()
    )

    assert "最低成本手段" in image_creator


def test_image_creator_limits_docker_to_read_only_base_image_probes():
    """Native validation, not Creator research, owns application builds."""
    image_creator = " ".join(
        (AGENTS_DIR / "image-creator.md").read_text().split()
    )

    assert "Docker 仅用于基础镜像的轻量只读查询" in image_creator
    assert "禁止在 `docker run` 中构建目标应用" in image_creator


def test_image_creator_writes_candidate_before_deferring_uncertainty():
    """An uncertain build fact must not postpone the first candidate."""
    image_creator = " ".join(
        (AGENTS_DIR / "image-creator.md").read_text().split()
    )

    assert "立即创建最小完整候选" in image_creator
    assert "由后续 `native_build` 和 `runtime_test` 验证" in image_creator


def test_image_creator_has_no_image_qa_evidence_contract():
    image_creator = (AGENTS_DIR / "image-creator.md").read_text().lower()

    assert "requirement_evidence_ids" not in image_creator
    assert '"evidence"' not in image_creator
    assert "禁止固定数字身份" in image_creator
    assert "mode: fixed" not in image_creator
    assert "单次研究网络操作最多 180 秒" in image_creator
    assert "不得加大超时反复重试" in image_creator
    assert "完整下载和校验交给后续原生构建" in image_creator
    assert "docker run --rm <基础镜像>" in image_creator
    assert "不要靠下载镜像产物或仓库元数据来推断" in image_creator
    assert "只获取到足以回答当前问题的程度" in image_creator
    assert "同一产物整轮只下载一次" in image_creator
    assert "不要靠加大下载量换取确定性" in image_creator


def test_image_creator_defines_the_existing_root_identity_contract():
    image_creator = " ".join(
        (AGENTS_DIR / "image-creator.md").read_text().lower().split()
    )
    assert "reuse_existing" in image_creator
    assert '"user": "root"' in image_creator
    assert '"group": "root"' in image_creator
    assert "直接使用基础镜像已有的 root" in image_creator


def test_image_creator_reuses_unchanged_upstream_assets_from_builder():
    image_creator = " ".join(
        (AGENTS_DIR / "image-creator.md").read_text().lower().split()
    )
    assert "固定版本的 builder 源码" in image_creator
    assert "未修改的上游附属文件" in image_creator
    assert "逐字节相同" in image_creator
    assert "必要的本地定制" in image_creator
    assert "复用策略" in image_creator
    assert "stage alias" in image_creator
    assert "copy --from" in image_creator



def test_image_creator_restricts_only_open_euler_owned_gitee_links():
    image_creator = (AGENTS_DIR / "image-creator.md").read_text().lower()

    assert "第三方" in image_creator
    assert "gitee.com/openeuler" in image_creator
    assert "gitee.com/src-openeuler" in image_creator
    assert "`gitee.com` 是 openeuler 迁移前的旧域名，一律禁止" not in image_creator


def test_testcase_prompts_keep_only_the_helper_optional():
    testcase_creator = (AGENTS_DIR / "testcase-creator.md").read_text().lower()
    testcase_qa = (AGENTS_DIR / "testcase-qa.md").read_text().lower()

    assert "`test_helpers.sh`：可选" in testcase_creator
    assert "optional `test_helpers.sh`" in testcase_qa
    assert "只允许" in testcase_creator


def test_testcase_prompts_leave_lifecycle_selection_to_native_harness():
    testcase_creator = " ".join(
        (AGENTS_DIR / "testcase-creator.md").read_text().lower().split()
    )
    testcase_qa = " ".join(
        (AGENTS_DIR / "testcase-qa.md").read_text().lower().split()
    )

    assert "harness 自动判断" in testcase_creator
    assert "不实现通用 readiness 循环" in testcase_creator
    assert "automatically selects the execution container" in testcase_qa
    assert "executes `test.sh` once" in testcase_qa
    assert "read back after restart by the harness" not in testcase_qa


def test_shared_prompts_derive_application_behavior_instead_of_assuming_it():
    image_creator = (AGENTS_DIR / "image-creator.md").read_text().lower()
    testcase_creator = (AGENTS_DIR / "testcase-creator.md").read_text().lower()
    testcase_qa = (AGENTS_DIR / "testcase-qa.md").read_text().lower()
    prompts = "\n".join((image_creator, testcase_creator, testcase_qa))

    assert "上游官方" in image_creator
    assert "dockerfile 与 official upstream" in " ".join(
        testcase_creator.split()
    )
    assert "if the image requires" in testcase_qa
    for fragment in ("uid 999", "tcp 6666", "redis-cli", "./x.py build"):
        assert fragment not in prompts


def test_image_creator_forbids_fixed_numeric_identity_without_semantic_review():
    image_creator = " ".join(
        (AGENTS_DIR / "image-creator.md").read_text().lower().split()
    )
    assert "groupadd -r" in image_creator
    assert "useradd -r" in image_creator
    assert "禁止固定数字身份" in image_creator
    assert '"evidence"' not in image_creator
    assert "原生构建" in image_creator


def test_fixer_cannot_reintroduce_fixed_numeric_identity():
    fixer = " ".join(
        (AGENTS_DIR / "code-fixer.md").read_text().lower().split()
    )

    assert "禁止固定数字 uid/gid" in fixer


def test_testcase_qa_cannot_report_image_only_defects():
    testcase_qa = " ".join(
        (AGENTS_DIR / "testcase-qa.md").read_text().lower().split()
    )

    assert "dockerfile is context only" in testcase_qa
    assert "actionable issues must identify a defect under `tests/`" in testcase_qa
    assert "image-only defect" in testcase_qa


def test_testcase_prompts_define_native_runtime_quality():
    creator = " ".join(
        (AGENTS_DIR / "testcase-creator.md").read_text().lower().split()
    )
    reviewer = " ".join(
        (AGENTS_DIR / "testcase-qa.md").read_text().lower().split()
    )

    for prompt in (creator, reviewer):
        assert "runtime_test" in prompt
        assert "test.sh" in prompt
        assert "真实" in prompt or "real" in prompt
        assert "协议" in prompt or "protocol" in prompt
        assert "数据路径" in prompt or "data path" in prompt


def test_testcase_creator_preserves_the_established_generation_workflow():
    """Removing one runtime backend must not erase unrelated test guidance."""
    creator = (AGENTS_DIR / "testcase-creator.md").read_text()

    for fragment in (
        "## 工作目录",
        "## 输入上下文",
        "### 步骤 1：分析 Dockerfile",
        "### 步骤 2：确定测试策略",
        "### 步骤 2b：核对每条应用命令的语义",
        "命令名相同不等于语义相同",
        "核对不通过或找不到权威出处时",
        "### 步骤 3：生成测试文件",
        "### 步骤 4：生成共享 test.sh",
        "### 步骤 5：返回结构化结果",
        "证据元数据不完整不会阻断 QA",
        '"package_name"',
        '"test_script_path"',
        '"binary_name"',
        '"expected_version"',
        '"exposed_ports"',
    ):
        assert fragment in creator
    assert '"test_type"' not in creator


def test_testcase_qa_preserves_the_established_adversarial_review_surfaces():
    """Lifecycle migration must retain the QA dimensions that catch bad tests."""
    reviewer = (AGENTS_DIR / "testcase-qa.md").read_text()

    for fragment in (
        "### Command Semantics (check this first)",
        "### Coverage Gaps",
        "### False Positive Risk",
        "### Missing Attack Surfaces",
        "### Test Correctness",
        "Are all attack angles covered?",
        "Could any test pass when the image is actually broken?",
        "What would break the image that these tests would NOT catch?",
        "Do file paths exist in the Dockerfile?",
        "SO_REUSEPORT",
        "dual-stack",
    ):
        assert fragment in reviewer


def test_fixer_forbids_runtime_and_test_false_passes():
    fixer = " ".join(
        (AGENTS_DIR / "code-fixer.md").read_text().lower().split()
    )

    for fragment in (
        "恒真 healthcheck",
        "sleep",
        "tail -f",
        "后台进程",
        "删除已声明端口",
        "弱化功能测试",
    ):
        assert fragment in fixer


def test_design_matches_the_advisory_testcase_evidence_model():
    design = (ROOT / "DESIGN.md").read_text()

    assert "`evidence_requests`" not in design
    assert "Creator 直接提交结构化 `evidence`" in design
    assert "证据不可用不阻断" in design


def test_design_describes_one_creator_repair_between_two_qa_reviews():
    design = (ROOT / "DESIGN.md").read_text()

    assert "QA1 → Creator 修正 → QA2" in design
    assert "Creator 再修正（第 2 轮）" not in design


def test_image_qa_prompt_is_removed():
    assert not (AGENTS_DIR / "image-qa.md").exists()


def test_docs_do_not_claim_image_creator_always_runs_once():
    design = (ROOT / "DESIGN.md").read_text()
    template = (ROOT / "templates" / "pr.md").read_text()

    assert "Image Creator 单次生成" not in design
    assert "single generation pass" not in template
    assert "Image QA" not in template


def test_image_creator_does_not_require_one_dockerfile_spelling_or_package_blacklist():
    image_creator = (AGENTS_DIR / "image-creator.md").read_text().lower()

    assert "禁止使用的包" not in image_creator
    assert "arg version 全大写" not in image_creator
    assert "version_filter 完整" not in image_creator


def test_testcase_prompts_keep_stateful_sequences_and_tests_in_sync():
    testcase_creator = (AGENTS_DIR / "testcase-creator.md").read_text().lower()
    testcase_qa = (AGENTS_DIR / "testcase-qa.md").read_text().lower()

    for prompt in (testcase_creator, testcase_qa):
        normalized = " ".join(prompt.split())
        assert "final dockerfile" in normalized
        assert "test.sh" in normalized
    assert "按顺序" in " ".join(testcase_creator.split())
    assert "stateful flow" in " ".join(testcase_qa.split())


def test_testcase_guidance_requires_real_protocol_quality():
    testcase_creator = (AGENTS_DIR / "testcase-creator.md").read_text().lower()
    testcase_qa = (AGENTS_DIR / "testcase-qa.md").read_text().lower()
    creator = " ".join(testcase_creator.split())
    assert "真实 http/应用协议" in creator
    assert "写入再读回" in creator
    assert "进程、端口、版本或文件存在" in creator

    reviewer = " ".join(testcase_qa.split())
    assert "real endpoint and meaningful response" in reviewer
    assert "real application protocol and data path" in reviewer
    assert "false-positive" in reviewer


def test_testcase_creator_example_emits_actionable_failure_diagnostics():
    creator = (AGENTS_DIR / "testcase-creator.md").read_text()
    example = creator.split("```bash", 1)[1].split("```", 1)[0]

    assert "version command exited" in example
    assert "version mismatch: expected=<%s> actual=<%s>" in example
    assert "core command exited" in example
    assert "core result mismatch: expected=<%s> actual=<%s>" in example
    assert "TESTS_FAILED" in example
    assert "|| true" not in example


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
        "failures",
        "failure_details",
        "container_evidence",
        "stdout_head",
        "probe",
        "full_probe",
    ):
        assert field in fixer
    # Rebuilding the runtime locally to read a log the container already holds
    # is what cost run 31106121623 its whole scratch budget.
    assert "不要为了复现" in fixer
    assert "逐项分类" in fixer
    assert "`failures` 是可选字段" in fixer
    assert "取决于失败类型" in fixer
    assert "按 `check` 逐项" in fixer
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


def test_testcase_qa_only_requests_repair_for_blocker_or_major_issues():
    reviewer = " ".join(
        (AGENTS_DIR / "testcase-qa.md").read_text().lower().split()
    )

    assert "if blocker or major issues remain after any repair round" in reviewer


def test_creator_prompts_keep_evidence_within_harness_bounds():
    prompt = " ".join((AGENTS_DIR / "testcase-creator.md").read_text().split())
    assert "最多 6" in prompt
    assert "1—2" in prompt
    assert "逐字" in prompt
    assert "TaskSpec 同源" in prompt
    assert "固定 revision" in prompt


def test_creator_provides_evidence_and_qa_reviews_it_without_a_gate():
    creators = (AGENTS_DIR / "testcase-creator.md").read_text()
    reviewers = (AGENTS_DIR / "testcase-qa.md").read_text()
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
    """Static checks own syntax; QA owns semantics and false positives."""
    creator = " ".join((AGENTS_DIR / "testcase-creator.md").read_text().split())
    reviewer = " ".join((AGENTS_DIR / "testcase-qa.md").read_text().split())

    assert "bash -n" in creator
    assert "Bash syntax" in reviewer
    assert "Review semantics and false-positive risk" in reviewer


def test_image_creator_can_report_unconfirmed_facts():
    """Retrying was the only contract-satisfying move when facts were required.

    An optional assumptions channel makes "not confirmed" a legal, cheap result,
    which is what removes the incentive to spend the budget on retries.
    """
    image_creator = " ".join(
        (AGENTS_DIR / "image-creator.md").read_text().split()
    )

    assert '"assumptions"' in image_creator
    assert "`assumptions` 是可选数组" in image_creator
    assert "不要为确认它而反复重试网络操作" in image_creator


def test_role_prompts_keep_single_run_specifics_out():
    """Role definitions hold durable rules; one run's details belong in evidence.

    Run 30872642022 tempted the opposite: naming the artifacts it downloaded and
    the command that read them would have encoded one application's packaging as
    a rule, and said nothing about the next application's.
    """
    prompts = "\n".join(
        path.read_text().lower()
        for path in sorted(AGENTS_DIR.glob("*.md"))
    )

    for fragment in (
        "kylin",
        "kvrocks",
        "sqlite",
        "repodata",
        "rootfs",
        "tar -tz",
        "tar.xz",
        "tarfile",
    ):
        assert fragment not in prompts
