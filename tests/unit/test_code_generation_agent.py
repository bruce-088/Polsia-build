"""Test CodeGenerationAgent — plan-only by default, file-content-grounded
proposal when given a real file_path, graceful fallback on fetch failure."""
from unittest.mock import patch

from app.agents.code_generation.agent import CodeGenerationAgent


def test_run_plan_only_when_no_file_path(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    agent = CodeGenerationAgent()
    result = agent.run({"title": "Add a health check", "task_metadata": None}, {})
    assert result["status"] == "planned"
    assert "new_content" not in result


def test_run_includes_real_file_content_in_prompt(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    agent = CodeGenerationAgent()

    captured_prompts = []

    def fake_call_claude_structured(prompt, **kwargs):
        captured_prompts.append(prompt)
        return {"summary": "ok", "file_path": "README.md", "new_content": "new", "status": "planned"}

    with patch("app.services.github_service.get_file_content", return_value="# Old README\n"):
        with patch.object(
            CodeGenerationAgent, "call_claude_structured", side_effect=fake_call_claude_structured
        ):
            result = agent.run(
                {"title": "Update README", "task_metadata": {"file_path": "README.md"}}, {}
            )

    assert len(captured_prompts) == 1
    assert "# Old README" in captured_prompts[0]
    assert result["file_path"] == "README.md"


def test_run_falls_back_to_plan_when_fetch_fails(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    agent = CodeGenerationAgent()

    captured_prompts = []

    def fake_call_claude_structured(prompt, **kwargs):
        captured_prompts.append(prompt)
        return {"summary": "ok", "status": "planned"}

    with patch(
        "app.services.github_service.get_file_content",
        side_effect=RuntimeError("GitHub is not configured"),
    ):
        with patch.object(
            CodeGenerationAgent, "call_claude_structured", side_effect=fake_call_claude_structured
        ):
            result = agent.run(
                {"title": "Update README", "task_metadata": {"file_path": "README.md"}}, {}
            )

    assert result["status"] == "planned"
    assert "not configured" in captured_prompts[0].lower()
