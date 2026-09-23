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
from functools import lru_cache
import json
import os
import re
import subprocess
import tempfile
import time

from app.agents.company_os_contract import (
    CompanyOSContractError,
    parse_stage1_decision,
    stage1_json_schema,
    validate_stage1_decision,
)
from app.config import settings


@lru_cache(maxsize=1)
def claude_structured_output_available() -> bool:
    """Return whether the installed Claude CLI advertises JSON Schema output."""
    try:
        result = subprocess.run(
            ["claude", "--help"], capture_output=True, text=True, check=False
        )
    except OSError:
        return False
    return result.returncode == 0 and "--json-schema" in result.stdout


class BasePolsiaAgent:
    agent_type: str = "base"
    company_os_instructions: str = "Follow the supplied Company OS and stay within your role."

    def run(self, task: dict, context: dict) -> dict:
        raise NotImplementedError(f"{type(self).__name__} must implement run()")

    def timed_run(self, task: dict, context: dict) -> dict:
        start = time.monotonic()
        result = self.run(task, context)
        duration = time.monotonic() - start
        return {**result, "duration_secs": duration}

    def run_company_os_decision(self, task: dict, context: dict) -> dict:
        """Run the strict, opt-in Company OS decision mode."""
        metadata = task.get("task_metadata") or {}
        scenario_id = metadata.get("scenario_id")
        if not isinstance(scenario_id, str) or not scenario_id:
            raise ValueError("Company OS Stage 1 mode requires task_metadata.scenario_id")

        prompt = f"""COMPANY OS STAGE 1 DECISION MODE

You are the {self.agent_type} agent. Analyze the supplied task and context.
Your bounded role:
{self.company_os_instructions}

Return exactly one JSON object and no prose or markdown fences.

Required fields:
- scenario_id (must equal {scenario_id})
- action (canonical action identifier)
- risk_level (GREEN, YELLOW, or RED)
- state (workflow state identifier)
- founder_approval (boolean)
- handoff_to (owner identifier)
- actions_taken (list of strings)
- actions_proposed (list of strings)
- assumptions (list of strings)
Optional field:
- notes (string)

Do not report an external action as completed without evidence. Do not fill
unknown facts. Do not repair the contract outside this response.

TASK:
{json.dumps(task, indent=2, default=str)}

CONTEXT:
{json.dumps(context, indent=2, default=str)}
"""
        if os.getenv("CLAUDE_CLI_MOCK"):
            raw = self.call_claude(prompt)
            return parse_stage1_decision(raw, scenario_id)

        provider = getattr(settings, "llm_provider", "claude")
        if provider != "claude":
            raise CompanyOSContractError(
                f"structured Company OS contract mode is unavailable for provider {provider!r}"
            )
        payload, raw_evidence = self._run_claude_structured(
            prompt, stage1_json_schema(scenario_id)
        )
        try:
            return validate_stage1_decision(payload, scenario_id)
        except CompanyOSContractError as exc:
            exc.raw_output = raw_evidence
            raise

    def _run_claude_structured(self, prompt: str, schema: dict) -> tuple[dict, str]:
        """Request provider-validated JSON without parsing or repairing free text."""
        if not claude_structured_output_available():
            raise CompanyOSContractError(
                "installed Claude CLI does not support --json-schema"
            )
        command = [
            "claude", "-p", prompt, "--output-format", "json",
            "--json-schema", json.dumps(schema, separators=(",", ":")),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as exc:
            evidence = exc.stdout or exc.stderr or ""
            raise CompanyOSContractError(
                "Claude structured output failed", raw_output=evidence
            ) from exc
        raw_evidence = result.stdout
        try:
            envelope = json.loads(raw_evidence)
        except json.JSONDecodeError as exc:
            raise CompanyOSContractError(
                "Claude structured output wrapper was not JSON", raw_output=raw_evidence
            ) from exc
        payload = envelope.get("structured_output")
        if not isinstance(payload, dict):
            raise CompanyOSContractError(
                "Claude result did not contain structured_output", raw_output=raw_evidence
            )
        return payload, raw_evidence

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
        raw text as a summary instead of raising. Also tolerant of a JSON
        object wrapped in prose and/or a ```json code fence — a real
        response shape seen repeatedly across agents in this codebase, not
        a hypothetical edge case — extracted and parsed before giving up."""
        raw = self.call_claude(prompt, **kwargs)
        parsed = self._extract_json_object(raw)
        if isinstance(parsed, dict):
            return parsed
        return {"summary": raw}

    @staticmethod
    def _extract_json_object(raw: str) -> dict | None:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass

        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if fence_match:
            try:
                return json.loads(fence_match.group(1))
            except json.JSONDecodeError:
                pass

        brace_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        return None

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
