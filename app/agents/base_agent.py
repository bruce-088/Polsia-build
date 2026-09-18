"""Base class every Polsia agent inherits from.

Mock contract (autouse in tests/conftest.py — do not break): when env var
CLAUDE_CLI_MOCK is truthy, call_claude() must not invoke any subprocess at
all; it returns CLAUDE_CLI_MOCK_RESPONSE's "result" field (JSON-decoded).

Provider-agnostic design: the Claude coupling lives entirely in
_run_llm_turn(). call_claude()/call_claude_json() are permanent-name wrappers
so nothing above this class (crew_factory, agent implementations, tests) has
to change if a second provider (e.g. OpenAI/Codex) is added later — only
_run_llm_turn's internals and settings.llm_provider would change.
"""
import json
import os
import subprocess
import tempfile
import time

from app.config import settings


class BasePolsiaAgent:
    agent_type: str = "base"

    def run(self, task: dict, context: dict) -> dict:
        raise NotImplementedError(f"{type(self).__name__} must implement run()")

    def timed_run(self, task: dict, context: dict) -> dict:
        start = time.monotonic()
        result = self.run(task, context)
        duration = time.monotonic() - start
        return {**result, "duration_secs": duration}

    def call_claude(self, prompt: str, **kwargs) -> str:
        if os.getenv("CLAUDE_CLI_MOCK"):
            raw = os.getenv("CLAUDE_CLI_MOCK_RESPONSE", json.dumps({"result": "Mock Claude response for testing"}))
            return json.loads(raw)["result"]
        return self._run_llm_turn(prompt, **kwargs)

    def call_claude_json(self, prompt: str, **kwargs) -> dict:
        return json.loads(self.call_claude(prompt, **kwargs))

    def call_claude_structured(self, prompt: str, **kwargs) -> dict:
        """Like call_claude_json, but tolerant of non-JSON responses (real LLM
        output isn't always clean JSON, and the default unit-test mock
        response is a plain string, not JSON) — falls back to wrapping the
        raw text as a summary instead of raising."""
        raw = self.call_claude(prompt, **kwargs)
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
            return {"summary": raw}
        except (json.JSONDecodeError, TypeError):
            return {"summary": raw}

    def _run_llm_turn(self, prompt: str, provider: str | None = None, **kwargs) -> str:
        """Real (non-mock) LLM call. Provider defaults to settings.llm_provider,
        but can be overridden per-call (used by call_dual_provider below to
        address Claude and OpenAI in the same turn)."""
        provider = provider or getattr(settings, "llm_provider", "claude")
        if provider == "claude":
            result = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "json"],
                capture_output=True,
                text=True,
                check=True,
            )
            return json.loads(result.stdout)["result"]
        if provider == "openai":
            with tempfile.TemporaryDirectory() as tmpdir:
                output_path = os.path.join(tmpdir, "codex_output.txt")
                subprocess.run(
                    [
                        "codex", "exec",
                        "--skip-git-repo-check", "--ephemeral",
                        "-o", output_path,
                        prompt,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=True,
                )
                with open(output_path) as f:
                    return f.read().strip()
        raise NotImplementedError(f"llm_provider {provider!r} is not wired up yet")

    def call_dual_provider(self, prompt: str, review_prompt_template: str | None = None) -> dict:
        """Have Claude and GPT (via the Codex CLI, OAuth-authenticated — no
        metered API key) collaborate on one task: Claude drafts, GPT
        reviews/critiques the draft, then Claude produces a final answer
        informed by that critique. This is the generic "two models working
        together" pattern — draft-then-review — not tied to any specific
        product; swap which provider drafts vs. reviews as needed.

        Falls back to a single Claude call if the codex CLI isn't installed
        or isn't authenticated in this environment, so this is always safe
        to call even where the Codex side isn't wired up.
        """
        draft = self.call_claude(prompt)

        review_prompt = (review_prompt_template or "Review this draft response and list concrete improvements:\n\n{draft}").format(
            draft=draft
        )
        try:
            critique = self._run_llm_turn(review_prompt, provider="openai")
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return {"draft": draft, "critique": None, "final": draft, "providers_used": ["claude"]}

        final_prompt = f"Original task:\n{prompt}\n\nYour draft:\n{draft}\n\nReviewer feedback:\n{critique}\n\nProduce an improved final answer incorporating the useful feedback."
        final = self.call_claude(final_prompt)

        return {"draft": draft, "critique": critique, "final": final, "providers_used": ["claude", "openai"]}
