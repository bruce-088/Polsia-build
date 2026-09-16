"""Customer support agent — drafts replies. Real LLM call for drafting; does
NOT send any reply automatically — always returns a draft for review."""
from app.agents.base_agent import BasePolsiaAgent
from app.services.company_service import build_context_prompt


class CustomerSupportAgent(BasePolsiaAgent):
    agent_type = "customer_support"

    def run(self, task: dict, context: dict) -> dict:
        context_block = build_context_prompt(context)
        prompt = f"""{context_block}

You are the customer support agent. Task: {task.get('title')}
{task.get('description') or ''}

Draft a helpful, on-brand reply to this inquiry. Respond with JSON only:
{{"summary": "<what you drafted, 1-2 sentences>",
"reply_draft": "<the reply text>", "status": "draft"}}"""

        result = self.call_claude_structured(prompt)
        result.setdefault("summary", "No reply drafted.")
        result["status"] = "draft"  # never auto-send; sending is unwired
        return result
