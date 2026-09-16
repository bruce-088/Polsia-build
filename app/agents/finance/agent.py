"""Finance agent — reviews/summarizes financial state. Real LLM call for
analysis; does NOT call live Stripe APIs to pull real balances (needs
credentials, a deliberate separate step — the Stripe *webhook* receiving
side is already wired up in app/api/v1/finance.py, but this agent doesn't
proactively poll Stripe)."""
from app.agents.base_agent import BasePolsiaAgent
from app.services.company_service import build_context_prompt


class FinanceAgent(BasePolsiaAgent):
    agent_type = "finance"

    def run(self, task: dict, context: dict) -> dict:
        context_block = build_context_prompt(context)
        prompt = f"""{context_block}

You are the finance agent. Task: {task.get('title')}
{task.get('description') or ''}

No live Stripe polling is connected yet (only incoming webhooks are wired),
so base your summary on the KPIs already in context. Respond with JSON only:
{{"summary": "<financial summary, 2-4 sentences>", "flags": []}}"""

        result = self.call_claude_structured(prompt)
        result.setdefault("summary", "No financial summary generated.")
        return result
