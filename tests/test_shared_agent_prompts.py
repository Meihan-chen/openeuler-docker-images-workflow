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
    image_creator = (AGENTS_DIR / "image-creator.md").read_text().lower()
    image_qa = (AGENTS_DIR / "image-qa.md").read_text().lower()

    for prompt in (image_creator, image_qa):
        assert "fixed numeric uid/gid" in prompt
        assert "base image or installed packages" in prompt
        assert "upstream or task contract" in prompt


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
    for prompt in (testcase_creator, testcase_qa):
        normalized = " ".join(prompt.split())
        assert "multiple listening sockets" in normalized
        assert "scalar" in normalized
        assert "functional" in normalized


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
