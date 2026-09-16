"""Code generation agent — proposes a plan/approach. Real LLM call for
planning; does NOT write code to a real repo or open a live GitHub PR (needs
PyGithub credentials + a real target repo, a deliberate separate step, and
arguably better served by a dedicated coding-agent engine like OpenHands
rather than this thin wrapper — see polsia-app-interface-spec.md)."""
from app.agents.base_agent import BasePolsiaAgent
from app.services.company_service import build_context_prompt


class CodeGenerationAgent(BasePolsiaAgent):
    agent_type = "code_generation"

    def run(self, task: dict, context: dict) -> dict:
        context_block = build_context_prompt(context)
        prompt = f"""{context_block}

You are the code generation agent. Task: {task.get('title')}
{task.get('description') or ''}

No live repository is connected yet, so describe an implementation plan
rather than producing a diff. Respond with JSON only:
{{"summary": "<plan, 2-4 sentences>", "status": "planned"}}"""

        result = self.call_claude_structured(prompt)
        result.setdefault("summary", "No plan generated.")
        result["status"] = "planned"  # never opens a real PR; that's unwired
        return result
