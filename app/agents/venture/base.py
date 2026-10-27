"""Shared plumbing for venture-lifecycle agents.

Venture agents receive the venture as a snapshot in
task["task_metadata"]["venture"] (written by venture_service when it creates
the task), so the existing task runner, context builder and Stage 2 paths
stay untouched.

Every venture agent post-processes the model's JSON deterministically:
unknown delegates are rejected, spend is routed to the founder, and an
"advance" recommendation is downgraded unless the lifecycle gate — recomputed
here from the snapshot, not taken from the model — actually passes.
"""
from __future__ import annotations

import json

from app.agents.base_agent import BasePolsiaAgent
from app.agents.venture import lifecycle
from app.services.company_service import build_context_prompt

MAX_DELEGATED_TASKS = 5
RECOMMENDATIONS = ("continue", "advance", "pivot", "kill", "pause")
FOUNDER_ONLY_RECOMMENDATIONS = ("pivot", "kill", "pause")


def venture_snapshot(task: dict) -> dict:
    metadata = task.get("task_metadata") or {}
    snapshot = metadata.get("venture")
    return snapshot if isinstance(snapshot, dict) else {}


def task_mode(task: dict, default: str) -> str:
    metadata = task.get("task_metadata") or {}
    mode = metadata.get("mode")
    return mode if isinstance(mode, str) and mode else default


def snapshot_phase(snapshot: dict) -> str:
    phase = snapshot.get("phase")
    return phase if phase in lifecycle.PHASES else lifecycle.PHASE_ORDER[0]


def recompute_gate(snapshot: dict) -> dict:
    """Gate evaluation from the snapshot's own metrics — never the model's
    opinion. Missing snapshot data fails closed."""
    phase = snapshot_phase(snapshot)
    overrides = (snapshot.get("gate_overrides") or {}).get(phase)
    try:
        return lifecycle.evaluate_gate(phase, snapshot.get("metrics"), overrides)
    except lifecycle.LifecycleError:
        return lifecycle.evaluate_gate(phase, snapshot.get("metrics"))


def venture_block(snapshot: dict) -> str:
    if not snapshot:
        return "VENTURE: none supplied. Do not invent one; say what is missing."
    phase = snapshot_phase(snapshot)
    gate = recompute_gate(snapshot)
    gate_lines = [
        f"  - {c['metric']}: {c['status']} (value={c['value']}, needs {c['op']} {c['threshold']:g})"
        for c in gate["criteria"]
    ]
    overrides = (snapshot.get("gate_overrides") or {}).get(phase)
    try:
        brief = lifecycle.phase_brief(phase, overrides)
    except lifecycle.LifecycleError:
        brief = lifecycle.phase_brief(phase)
    lines = [
        "VENTURE (separate from any company configured above; use that only as background):",
        f"Name: {snapshot.get('name')}",
        f"Idea: {snapshot.get('idea')}",
        f"Target customer: {snapshot.get('target_customer')}",
        f"Business model: {snapshot.get('business_model')}",
        f"Status: {snapshot.get('status')}",
        brief,
        f"Gate status right now ({gate['gate']}: {'PASSED' if gate['passed'] else 'NOT PASSED'}):",
        *gate_lines,
        "Recorded metrics (verified=false means nobody has confirmed it):",
        json.dumps(snapshot.get("metrics") or {}, default=str),
        "Idea scores:",
        json.dumps(snapshot.get("scores") or {}, default=str),
        "Recent history:",
        json.dumps((snapshot.get("history") or [])[-5:], default=str),
    ]
    return "\n".join(lines)


GROUND_RULES = """Ground rules:
- Use only facts in the task, venture snapshot, or cited evidence. Never invent
  customers, metrics, sources, quotes or completed actions.
- You produce proposals only. You do not send, publish, spend, deploy, sign,
  change prices, or change the venture's phase.
- Anything that spends money, contacts real people, publishes, or commits the
  business is a founder (RED) decision: list it under founder_decisions.
- Prefer the cheapest, fastest test that could prove an assumption wrong."""


class VentureAgentBase(BasePolsiaAgent):
    """Common prompt framing for every venture-lifecycle agent."""

    role_title: str = "venture agent"

    def framing(self, task: dict, context: dict) -> str:
        return "\n\n".join(
            [
                build_context_prompt(context),
                venture_block(venture_snapshot(task)),
                f"You are the {self.role_title}. Task: {task.get('title')}\n{task.get('description') or ''}",
                GROUND_RULES,
            ]
        )

    @staticmethod
    def clean_founder_decisions(raw: object) -> list[dict]:
        decisions = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict) and str(item.get("decision") or "").strip():
                decisions.append(
                    {
                        "decision": str(item["decision"]).strip(),
                        "why": str(item.get("why") or "").strip(),
                        "risk_level": "RED",
                    }
                )
            elif isinstance(item, str) and item.strip():
                decisions.append({"decision": item.strip(), "why": "", "risk_level": "RED"})
        return decisions

    @staticmethod
    def filter_delegations(raw: object, allowed: tuple[str, ...], limit: int) -> tuple[list[dict], list[dict]]:
        """Keep delegations to allowed agent types; report the rest."""
        accepted: list[dict] = []
        rejected: list[dict] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                rejected.append({"item": item, "reason": "not an object"})
                continue
            agent_type = item.get("agent_type")
            title = str(item.get("title") or "").strip()
            if agent_type not in allowed:
                rejected.append({**item, "reason": f"{agent_type!r} is not a permitted delegate here"})
            elif not title:
                rejected.append({**item, "reason": "missing title"})
            elif len(accepted) >= limit:
                rejected.append({**item, "reason": f"over the {limit}-task limit for one plan"})
            else:
                accepted.append(
                    {
                        "agent_type": agent_type,
                        "title": title[:500],
                        "description": str(item.get("description") or "").strip(),
                        **({"mode": item["mode"]} if isinstance(item.get("mode"), str) else {}),
                        **(
                            {"success_metric": str(item["success_metric"])}
                            if item.get("success_metric")
                            else {}
                        ),
                    }
                )
        return accepted, rejected

    @staticmethod
    def guard_recommendation(raw: object, gate: dict, decisions: list[dict]) -> tuple[str, list[str]]:
        """Normalize a phase recommendation. 'advance' only survives when the
        deterministic gate passed; pivot/kill/pause are always founder
        decisions."""
        notes: list[str] = []
        recommendation = raw if raw in RECOMMENDATIONS else "continue"
        if raw not in RECOMMENDATIONS and raw is not None:
            notes.append(f"unknown recommendation {raw!r} treated as 'continue'")
        if recommendation == "advance" and not gate["passed"]:
            notes.append(
                "advance blocked: gate not passed "
                f"(failing={gate['failing']}, missing={gate['missing']}, unverified={gate['unverified']})"
            )
            recommendation = "continue"
        if recommendation == "advance" and gate.get("next_phase") is None:
            notes.append("advance blocked: already in the final phase")
            recommendation = "continue"
        if recommendation == "advance":
            decisions.append(
                {
                    "decision": f"Advance to {gate['next_phase']}",
                    "why": f"{gate['gate']} gate passed on verified metrics",
                    "risk_level": "RED",
                }
            )
        if recommendation in FOUNDER_ONLY_RECOMMENDATIONS:
            decisions.append(
                {"decision": f"{recommendation.capitalize()} this venture", "why": "recommended by agent", "risk_level": "RED"}
            )
        return recommendation, notes


class VenturePhaseLeadAgent(VentureAgentBase):
    """A phase lead plans the phase and delegates to that phase's workers."""

    phase_key: str = "research_validation"
    role_focus: str = ""

    def run(self, task: dict, context: dict) -> dict:
        snapshot = venture_snapshot(task)
        phase = lifecycle.get_phase(self.phase_key)
        workers = lifecycle.lead_dispatchable(self.phase_key)
        prompt = f"""{self.framing(task, context)}

Your phase focus:
{self.role_focus}

Delegate concrete work only to these worker agents: {", ".join(workers)}.
Each delegated task must be self-contained (the worker sees only its title and
description) and name the metric it moves.

Respond with JSON only:
{{"summary": "<2-4 sentences>",
"assessment": "<where the venture stands against the {phase.exit_gate_name} gate and why>",
"proposed_tasks": [{{"agent_type": "<one of the workers>", "title": "<task>",
  "description": "<full brief>", "success_metric": "<gate metric it moves>"}}],
"experiments": [{{"hypothesis": "<falsifiable>", "method": "<test>",
  "metric": "<metric>", "pass_threshold": "<set before running>",
  "budget_usd": <number>, "duration_days": <number>}}],
"founder_decisions": [{{"decision": "<RED decision needed>", "why": "<reason>"}}],
"recommendation": "continue|advance|pivot|kill|pause",
"risks": ["<risk>"]}}"""
        result = self.call_claude_structured(prompt)
        return self.postprocess(result, snapshot)

    def postprocess(self, result: dict, snapshot: dict) -> dict:
        gate = recompute_gate({**snapshot, "phase": self.phase_key})
        decisions = self.clean_founder_decisions(result.get("founder_decisions"))
        accepted, rejected = self.filter_delegations(
            result.get("proposed_tasks"), lifecycle.lead_dispatchable(self.phase_key), MAX_DELEGATED_TASKS
        )
        experiments = []
        for item in result.get("experiments") if isinstance(result.get("experiments"), list) else []:
            if not isinstance(item, dict):
                continue
            budget = lifecycle.coerce_metric_value(item.get("budget_usd"))
            spends = budget is None or budget > 0
            experiments.append({**item, "requires_founder_approval": spends})
            if spends:
                decisions.append(
                    {
                        "decision": f"Approve spend for experiment: {item.get('hypothesis') or item.get('method')}",
                        "why": "experiment has a non-zero or unknown budget",
                        "risk_level": "RED",
                    }
                )
        recommendation, notes = self.guard_recommendation(result.get("recommendation"), gate, decisions)
        return {
            "summary": result.get("summary") or f"No {self.phase_key} plan generated.",
            "assessment": result.get("assessment"),
            "phase": self.phase_key,
            "proposed_tasks": accepted,
            "rejected_tasks": rejected,
            "experiments": experiments,
            "founder_decisions": decisions,
            "recommendation": recommendation,
            "guard_notes": notes,
            "risks": result.get("risks") if isinstance(result.get("risks"), list) else [],
            "gate": gate,
            "status": "proposal",
        }
