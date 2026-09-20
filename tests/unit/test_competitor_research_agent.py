"""Test CompetitorResearchAgent — real search grounding + graceful fallback
when Tavily isn't configured or a search call fails."""
from unittest.mock import patch

from app.agents.competitor_research.agent import CompetitorResearchAgent


def test_run_falls_back_gracefully_when_tavily_not_configured(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    agent = CompetitorResearchAgent()
    result = agent.run({"title": "Find HVAC prospects in Orlando"}, {})
    assert "summary" in result
    assert result["prospects"] == []


def test_run_includes_real_search_results_in_prompt(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    agent = CompetitorResearchAgent()

    fake_results = [
        {
            "title": "ABC Heating & Air",
            "url": "https://abcheating.com",
            "content": "Full service HVAC company in Orlando, FL.",
        }
    ]
    captured_prompts = []

    def fake_call_claude_structured(prompt, **kwargs):
        captured_prompts.append(prompt)
        return {"summary": "ok", "prospects": []}

    with patch("app.services.research_service.search_web", return_value=fake_results):
        with patch.object(
            CompetitorResearchAgent, "call_claude_structured", side_effect=fake_call_claude_structured
        ):
            agent.run({"title": "Find HVAC prospects in Orlando"}, {})

    assert len(captured_prompts) == 1
    assert "ABC Heating & Air" in captured_prompts[0]
    assert "https://abcheating.com" in captured_prompts[0]


def test_run_notes_search_failure_without_crashing(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    agent = CompetitorResearchAgent()

    captured_prompts = []

    def fake_call_claude_structured(prompt, **kwargs):
        captured_prompts.append(prompt)
        return {"summary": "ok", "prospects": []}

    with patch(
        "app.services.research_service.search_web",
        side_effect=RuntimeError("Tavily is not configured (tavily_api_key)"),
    ):
        with patch.object(
            CompetitorResearchAgent, "call_claude_structured", side_effect=fake_call_claude_structured
        ):
            result = agent.run({"title": "Find HVAC prospects"}, {})

    assert "summary" in result
    assert "unavailable" in captured_prompts[0].lower()
