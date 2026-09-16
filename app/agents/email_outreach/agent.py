"""Email outreach agent — drafts cold-outreach copy. Real LLM call for
drafting; does NOT send via SendGrid (needs credentials, a deliberate
separate step) — always returns drafts, never marks anything as sent."""
from app.agents.base_agent import BasePolsiaAgent
from app.services.company_service import build_context_prompt


class EmailOutreachAgent(BasePolsiaAgent):
    agent_type = "email_outreach"

    def run(self, task: dict, context: dict) -> dict:
        context_block = build_context_prompt(context)
        prompt = f"""{context_block}

You are the email outreach agent. Task: {task.get('title')}
{task.get('description') or ''}

Draft a short, non-spammy cold outreach email. Respond with JSON only:
{{"summary": "<what you drafted, 1-2 sentences>",
"subject": "<subject line>", "body": "<email body>", "status": "draft"}}"""

        result = self.call_claude_structured(prompt)
        result.setdefault("summary", "No email drafted.")
        result["status"] = "draft"  # never auto-send; sending is unwired
        return result
