"""Code generation agent — proposes a plan. When triggered with a
file_path (POST /agents/code_generation/trigger's optional field, carried
via Task.task_metadata), it instead fetches that file's real current
content from GitHub and proposes the complete new content grounded in
what's actually there. Never writes to the repo or opens a PR itself —
that's a separate, explicitly human-triggered action
(app/api/v1/code.py's POST /open-pr, which the human calls after
reviewing this agent's proposed content)."""
from app.agents.base_agent import BasePolsiaAgent
from app.services.company_service import build_context_prompt


class CodeGenerationAgent(BasePolsiaAgent):
    agent_type = "code_generation"

    def run(self, task: dict, context: dict) -> dict:
        context_block = build_context_prompt(context)
        file_path = (task.get("task_metadata") or {}).get("file_path")

        if not file_path:
            return self._plan_only(context_block, task, note=None)

        try:
            from app.services.github_service import get_file_content

            current_content = get_file_content(file_path)
        except Exception as exc:
            return self._plan_only(
                context_block, task, note=f"Could not fetch {file_path} ({exc})."
            )

        prompt = f"""{context_block}

You are the code generation agent. Task: {task.get('title')}
{task.get('description') or ''}

Current content of {file_path}:
---
{current_content}
---

Produce the FULL new content of this file incorporating the requested
change — the complete file, not a diff or partial snippet. Make the
smallest change that accomplishes the task; don't refactor unrelated
code. Respond with JSON only:
{{"summary": "<what changed, 1-2 sentences>",
"file_path": "{file_path}",
"new_content": "<the complete new file content>",
"pr_title": "<a short PR title>",
"pr_body": "<a short PR description explaining the change and why>",
"status": "planned"}}"""

        result = self.call_claude_structured(prompt)
        result.setdefault("summary", "No plan generated.")
        result["status"] = "planned"  # never opens a real PR itself; see module docstring
        return result

    def _plan_only(self, context_block: str, task: dict, note: str | None) -> dict:
        note_line = note or "No specific file was given, so describe an implementation plan rather than producing file content."
        prompt = f"""{context_block}

You are the code generation agent. Task: {task.get('title')}
{task.get('description') or ''}

{note_line} Respond with JSON only:
{{"summary": "<plan, 2-4 sentences>", "status": "planned"}}"""

        result = self.call_claude_structured(prompt)
        result.setdefault("summary", "No plan generated.")
        result["status"] = "planned"
        return result
