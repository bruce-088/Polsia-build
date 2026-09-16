"""Social media agent — drafts posts. Real LLM call for drafting; does NOT
post live to Twitter/X (needs tweepy credentials, a deliberate separate
step) — always returns drafts with status "draft" regardless of
settings.sandbox_mode, since posting was never wired up to begin with."""
from app.agents.base_agent import BasePolsiaAgent
from app.services.company_service import build_context_prompt


class SocialMediaAgent(BasePolsiaAgent):
    agent_type = "social_media"

    def run(self, task: dict, context: dict) -> dict:
        context_block = build_context_prompt(context)
        prompt = f"""{context_block}

You are the social media agent. Task: {task.get('title')}
{task.get('description') or ''}

Draft 1-3 tweets (each under 280 characters) that fit the company's voice
and mission. Respond with JSON only:
{{"summary": "<what you drafted, 1-2 sentences>",
"tweets": [{{"content": "<tweet text>", "status": "draft"}}]}}"""

        result = self.call_claude_structured(prompt)
        result.setdefault("summary", "No posts drafted.")
        result.setdefault("tweets", [])
        for tweet in result.get("tweets", []):
            tweet["status"] = "draft"  # never auto-publish; posting is unwired
        return result
