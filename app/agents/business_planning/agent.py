"""Business planning agent — strategy & KPI review. Real LLM call; no
external side effects (this agent only ever produces recommendations, it
doesn't need live integrations)."""
from app.agents.base_agent import BasePolsiaAgent
from app.services.company_service import build_context_prompt


class BusinessPlanningAgent(BasePolsiaAgent):
    agent_type = "business_planning"

    def run(self, task: dict, context: dict) -> dict:
        context_block = build_context_prompt(context)
        prompt = f"""{context_block}

You are the business planning agent. Task: {task.get('title')}
{task.get('description') or ''}

Review the company's current KPIs and goals, and propose the single highest-
leverage next move. Respond with JSON only:
{{"summary": "<recommendation, 2-4 sentences>",
"proposed_goals": {{}}, "priority": "high|medium|low"}}"""

        result = self.call_claude_structured(prompt)
        result.setdefault("summary", "No recommendation generated.")
        return result
