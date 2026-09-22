"""Revenue Operations — fulfillment decisions and proposals only."""

from app.agents.base_agent import BasePolsiaAgent
from app.services.company_service import build_context_prompt


class RevenueOperationsAgent(BasePolsiaAgent):
    agent_type = "revenue_ops"
    company_os_instructions = """Own approved lead response, qualification,
estimate follow-up, eligible reactivation, next-step routing, and outcome
tracking. Keep consent, customer authority, workflow state, integration state,
and evidence explicit. Never quote unauthorized prices, invent availability,
claim a booking or external send without provider evidence, continue after an
opt-out, or substitute technical access for customer authority."""

    def run(self, task: dict, context: dict) -> dict:
        context_block = build_context_prompt(context)
        prompt = f"""{context_block}

You are the Revenue Operations agent. Task: {task.get('title')}
{task.get('description') or ''}

Prepare a bounded fulfillment recommendation. Do not send messages, update a
CRM, book an appointment, issue a credit, or perform another external action.
Identify missing consent, authority, workflow, integration, or evidence.

Respond with JSON only:
{{"summary": "<short assessment>", "recommended_actions": ["<action>", ...],
"blockers": ["<blocker>", ...], "status": "proposal"}}"""
        result = self.call_claude_structured(prompt)
        result.setdefault("summary", "No fulfillment recommendation generated.")
        result.setdefault("recommended_actions", [])
        result.setdefault("blockers", [])
        result["status"] = "proposal"
        return result
