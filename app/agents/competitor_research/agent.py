"""Competitor/market research agent. Uses real web search via Tavily
(app/services/research_service.search_web) to ground findings in current
data when configured; falls back to LLM-only reasoning (with that
limitation stated explicitly, never silently) when it isn't or a search
call fails. Also doubles as prospect discovery/qualification — Acqivo's
"Market Intelligence" role covers both."""
from app.agents.base_agent import BasePolsiaAgent
from app.services.company_service import build_context_prompt


class CompetitorResearchAgent(BasePolsiaAgent):
    agent_type = "competitor_research"

    def run(self, task: dict, context: dict) -> dict:
        context_block = build_context_prompt(context)

        query = task.get("title") or "market research"
        if task.get("description"):
            query = f"{query} — {task['description']}"

        try:
            from app.services.research_service import search_web

            results = search_web(query)
            if results:
                search_results_block = "\n\n".join(
                    f"Source: {r['url']}\nTitle: {r['title']}\nContent: {r['content']}"
                    for r in results
                )
            else:
                search_results_block = "Web search ran but returned no results."
        except Exception as exc:
            search_results_block = f"Web search unavailable ({exc}). Answering from general knowledge only."

        prompt = f"""{context_block}

You are the competitor/market research agent. Task: {task.get('title')}
{task.get('description') or ''}

Real web search results:
{search_results_block}

Ground every factual claim in the search results above — do not invent
companies, facts, or data points not backed by a source. If search results
aren't present or don't cover the question, say so explicitly rather than
guessing.

Respond with JSON only:
{{"summary": "<findings, 2-4 sentences>",
"competitor_name": "<name or null, for competitor-analysis tasks>",
"positioning": "<short note or null>",
"prospects": [{{"company_name": "<name>", "market": "<city, state>",
"industry": "<industry>", "website": "<url or null>", "services": [],
"public_signals": [], "lead_flow_signals": [],
"follow_up_gap_hypothesis": "<string or null — a hypothesis, not a claimed fact>",
"evidence": [{{"source": "<url>", "claim": "<what it shows>"}}],
"score": <integer 0-12>, "confidence": "low|medium|high",
"recommended_pitch": "<string or null>"}}]}}

Leave "prospects" an empty list unless the task is specifically about
finding or qualifying prospect companies."""

        result = self.call_claude_structured(prompt)
        result.setdefault("summary", "No research generated.")
        result.setdefault("prospects", [])
        return result
