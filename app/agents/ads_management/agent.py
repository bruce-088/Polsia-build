"""Ads management agent — proposes optimizations. Real LLM call for
analysis; does NOT call live Google Ads/Meta Ads APIs (needs real ad-account
credentials, a deliberate separate step)."""
from app.agents.base_agent import BasePolsiaAgent
from app.services.company_service import build_context_prompt


class AdsManagementAgent(BasePolsiaAgent):
    agent_type = "ads_management"

    def run(self, task: dict, context: dict) -> dict:
        context_block = build_context_prompt(context)
        prompt = f"""{context_block}

You are the ads management agent. Task: {task.get('title')}
{task.get('description') or ''}

No live ad account data is connected yet, so propose general optimization
priorities rather than specific budget changes. Respond with JSON only:
{{"summary": "<recommendation, 2-4 sentences>", "recommended_actions": []}}"""

        result = self.call_claude_structured(prompt)
        result.setdefault("summary", "No ads recommendation generated.")
        return result
