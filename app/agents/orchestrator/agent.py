"""Orchestrator — scheduler-only (daily_cycle.py), not user-triggerable via
the API. Produces the morning plan / evening summary. Real LLM call via
call_claude_structured; no external side effects."""
from app.agents.base_agent import BasePolsiaAgent
from app.services.company_service import build_context_prompt


class OrchestratorAgent(BasePolsiaAgent):
    agent_type = "orchestrator"

    def run(self, task: dict, context: dict) -> dict:
        context_block = build_context_prompt(context)
        title = (task.get("title") or "").lower()

        if "evening" in title or "summary" in title:
            prompt = f"""{context_block}

You are the orchestrator agent, writing today's evening summary for the
company above. Review what today's tasks were and how they went. Respond
with JSON only: {{"summary": "<2-4 sentence recap>",
"insights": ["<short insight>", ...]}}"""
        else:
            prompt = f"""{context_block}

You are the orchestrator agent, writing this morning's plan for the company
above. Decide what the business needs most today given its KPIs and
yesterday's summary. Respond with JSON only:
{{"summary": "<the plan, 2-4 sentences>", "insights": []}}"""

        result = self.call_claude_structured(prompt)
        result.setdefault("summary", "No plan generated.")
        result.setdefault("insights", [])
        return result
