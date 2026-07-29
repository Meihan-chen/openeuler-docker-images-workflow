from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = ROOT / ".github" / "agents"


def test_scenario_one_reuses_the_shared_agent_definitions():
    from scripts.harness.run import build_role_prompt
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
