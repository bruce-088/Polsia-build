"""Competitor research agent. Real LLM call for analysis/write-up; does NOT
call a live web-search API (e.g. Tavily) — that needs a TAVILY_API_KEY and
is a separate, deliberate integration step, not wired up here."""
from app.agents.base_agent import BasePolsiaAgent
from app.services.company_service import build_context_prompt


class CompetitorResearchAgent(BasePolsiaAgent):
    agent_type = "competitor_research"

    def run(self, task: dict, context: dict) -> dict:
        context_block = build_context_prompt(context)
        prompt = f"""{context_block}

You are the competitor research agent. Task: {task.get('title')}
{task.get('description') or ''}

Analyze based on what you already know (no live web search is wired up yet
— note this limitation in your summary if the request needs current data).
Respond with JSON only:
{{"summary": "<findings, 2-4 sentences>", "competitor_name": "<name or null>",
"positioning": "<short note or null>"}}"""

        result = self.call_claude_structured(prompt)
        result.setdefault("summary", "No research generated.")
        return result
