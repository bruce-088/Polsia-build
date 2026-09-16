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

    def _run_llm_turn(self, prompt: str, **kwargs) -> str:
        """Real (non-mock) LLM call. Provider chosen by settings.llm_provider —
        this is the only place that needs to change to add a second backend."""
        provider = getattr(settings, "llm_provider", "claude")
        if provider == "claude":
            result = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "json"],
                capture_output=True,
                text=True,
                check=True,
            )
            return json.loads(result.stdout)["result"]
        raise NotImplementedError(f"llm_provider {provider!r} is not wired up yet")
