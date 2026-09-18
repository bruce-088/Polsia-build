"""Finance agent — reviews/summarizes financial state. When
settings.stripe_secret_key is configured, the Celery task that dispatches
this agent (celery_app/tasks/agent_tasks.py) polls live Stripe balance/MRR
first (read-only: Balance.retrieve + Subscription.list, never writes to
Stripe) and merges it into context["kpis"] before this runs — so the prompt
below always reflects whatever KPIs are in context, live or not, without
this file needing to know which. Falls back to context-only summary if no
Stripe key is set."""
from app.agents.base_agent import BasePolsiaAgent
from app.services.company_service import build_context_prompt


class FinanceAgent(BasePolsiaAgent):
    agent_type = "finance"

    def run(self, task: dict, context: dict) -> dict:
        context_block = build_context_prompt(context)
        prompt = f"""{context_block}

You are the finance agent. Task: {task.get('title')}
{task.get('description') or ''}

Base your summary on the KPIs already in context above (mrr_cents,
arr_cents, active_subscribers, stripe_balance_cents are live Stripe figures
when present). Respond with JSON only:
{{"summary": "<financial summary, 2-4 sentences>", "flags": []}}"""

        result = self.call_claude_structured(prompt)
        result.setdefault("summary", "No financial summary generated.")
        return result
