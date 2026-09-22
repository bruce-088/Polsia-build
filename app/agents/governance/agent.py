"""Governance — policy, resilience, and recovery decisions only."""

from app.agents.base_agent import BasePolsiaAgent
from app.services.company_service import build_context_prompt


class GovernanceAgent(BasePolsiaAgent):
    agent_type = "governance"
    company_os_instructions = """Own policy classification, approval routing,
integration-failure containment, bounded retry/recovery review, audit gaps, and
cross-agent governance conflicts. Fail closed when authority or evidence is
missing. Never approve your own RED action, invent provider success, switch to
an unapproved provider, discard queued work, expand an existing approval, or
execute the governed action."""

    def run(self, task: dict, context: dict) -> dict:
        context_block = build_context_prompt(context)
        prompt = f"""{context_block}

You are the Governance and Resilience agent. Task: {task.get('title')}
{task.get('description') or ''}

Assess the policy or resilience issue and propose containment, escalation, or
recovery steps. Do not execute retries, integrations, approvals, or external
actions. Preserve uncertainty and identify required evidence.

Respond with JSON only:
{{"summary": "<short assessment>", "recommended_actions": ["<action>", ...],
"blockers": ["<blocker>", ...], "status": "proposal"}}"""
        result = self.call_claude_structured(prompt)
        result.setdefault("summary", "No governance recommendation generated.")
        result.setdefault("recommended_actions", [])
        result.setdefault("blockers", [])
        result["status"] = "proposal"
        return result
