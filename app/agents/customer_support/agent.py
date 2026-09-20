"""Customer support agent — drafts replies. Real LLM call for drafting;
does NOT send any reply itself. When a reply came from the real inbound
sweep (celery_app/tasks/agent_tasks.py), the caller may auto-send this
draft on the agent's behalf, but only after
app/services/auto_send_policy.py's independent, multi-layer gate agrees —
this agent's own auto_send_eligible field is just one input to that gate,
never sufficient by itself."""
from app.agents.base_agent import BasePolsiaAgent
from app.services.company_service import build_context_prompt


class CustomerSupportAgent(BasePolsiaAgent):
    agent_type = "customer_support"

    def run(self, task: dict, context: dict) -> dict:
        context_block = build_context_prompt(context)
        prompt = f"""{context_block}

You are the customer support agent. Task: {task.get('title')}
{task.get('description') or ''}

Draft a helpful, on-brand reply to this inquiry.

Also assess whether this reply is safe to send automatically without human
review. Only mark "auto_send_eligible": true for simple, unambiguous
factual questions (e.g. "what does this cost", "how does this work",
"do you cover my area") where your answer makes no commitments, promises,
or exceptions. Mark it false for anything involving pricing negotiation,
discounts, refunds, cancellations, complaints, legal/compliance questions,
or any inquiry where you're not fully confident in the answer. When in
doubt, mark it false — a human reviewing an unnecessary draft costs
nothing; an unreviewed bad reply going out for real does.

Respond with JSON only:
{{"summary": "<what you drafted, 1-2 sentences>",
"reply_draft": "<the reply text>",
"auto_send_eligible": <true or false>,
"status": "draft"}}"""

        result = self.call_claude_structured(prompt)
        result.setdefault("summary", "No reply drafted.")
        result.setdefault("auto_send_eligible", False)
        result["status"] = "draft"  # this agent never sends; see module docstring
        return result
