"""Deterministic venture lifecycle: phases, playbooks, exit gates, dispatch
rules and the idea-scoring rubric.

Pure data + pure functions — no LLM, no DB, no I/O. Gate decisions and
arithmetic live here so no model can talk a gate into passing or fudge a
weighted score. Agents *read* this module to build their prompts; the
venture service *enforces* it when applying agent output.

Gate rule (mirrors the validation program's "advance only when evidence
satisfies the gate"): a gate passes only when every criterion has a
verified metric that meets its threshold. Missing or unverified evidence
fails closed.
"""
from __future__ import annotations

import math
import operator
import re
from dataclasses import dataclass

COMMANDER = "commander"
TRACKER = "tracker"
PREDICTOR = "predictor"

PHASE_ORDER: tuple[str, ...] = ("research_validation", "launch", "marketing", "scaling")

VENTURE_AGENT_TYPES: tuple[str, ...] = (COMMANDER, TRACKER, PREDICTOR, *PHASE_ORDER)

VENTURE_STATUSES: tuple[str, ...] = ("exploring", "active", "paused", "killed", "graduated")

METRIC_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

_OPS = {">=": operator.ge, "<=": operator.le}


@dataclass(frozen=True)
class GateCriterion:
    metric: str
    op: str
    threshold: float
    description: str

    def as_dict(self) -> dict:
        return {
            "metric": self.metric,
            "op": self.op,
            "threshold": self.threshold,
            "description": self.description,
        }


@dataclass(frozen=True)
class PhaseSpec:
    key: str
    name: str
    lead_agent: str
    objective: str
    playbook: tuple[str, ...]
    worker_agents: tuple[str, ...]
    founder_decisions: tuple[str, ...]
    exit_gate_name: str
    exit_gate: tuple[GateCriterion, ...]


PHASES: dict[str, PhaseSpec] = {
    "research_validation": PhaseSpec(
        key="research_validation",
        name="Research & Validation",
        lead_agent="research_validation",
        objective=(
            "Decide with evidence whether this idea deserves a launch: a real, "
            "painful problem, reachable buyers, and committed demand."
        ),
        playbook=(
            "Turn the riskiest assumptions (problem, buyer, willingness to pay, reachability) into testable hypotheses.",
            "Collect independent problem evidence: complaints, reviews, forum threads, search demand, job posts.",
            "Map at least three competitors or substitutes and how buyers solve the problem today.",
            "Define the ICP: who buys, who uses, who owns the budget, where they gather online.",
            "Design a smoke test (landing page, fake door, waitlist or pre-sale) with a pass threshold set before it runs.",
            "Turn interest into commitment: pre-orders, deposits, letters of intent or booked discovery calls.",
            "Recommend proceed, pivot or kill, citing the evidence.",
        ),
        worker_agents=(
            "competitor_research",
            "business_planning",
            "code_generation",
            "social_media",
            "email_outreach",
            "finance",
        ),
        founder_decisions=(
            "Spending any money on a smoke test (ads, tools, domains)",
            "Publishing a landing page or taking pre-orders or payments",
            "Contacting real people for interviews or outreach",
            "Pivoting or killing the idea",
            "Advancing to Launch",
        ),
        exit_gate_name="Validated",
        exit_gate=(
            GateCriterion("problem_evidence_sources", ">=", 5, "Independent sources documenting the problem"),
            GateCriterion("competitors_analyzed", ">=", 3, "Competitors or substitutes analyzed"),
            GateCriterion("icp_defined", ">=", 1, "ICP written and accepted (1 = yes)"),
            GateCriterion("smoke_test_visitors", ">=", 100, "Targeted visitors to the smoke-test page"),
            GateCriterion("smoke_test_conversion_rate", ">=", 0.05, "Visitor-to-signup conversion on the smoke test"),
            GateCriterion("committed_signals", ">=", 3, "Pre-orders, deposits, LOIs or booked discovery calls"),
        ),
    ),
    "launch": PhaseSpec(
        key="launch",
        name="Launch",
        lead_agent="launch",
        objective=(
            "Ship the smallest offer that delivers the core outcome, take real "
            "payment, and get the first customers to value."
        ),
        playbook=(
            "Scope the MVP to the one outcome validation showed buyers want; list what is explicitly out.",
            "Propose the offer: price, packaging, terms and onboarding steps.",
            "Build the product surface, landing page and checkout through pull requests, never direct deploys.",
            "Verify checkout end to end in test mode before any live activation.",
            "Convert validation leads (waitlist, pre-orders) into the first paying customers.",
            "Define the first-value moment and measure activation against it.",
            "Run a launch retro: defects, onboarding friction, objections.",
        ),
        worker_agents=(
            "code_generation",
            "business_planning",
            "email_outreach",
            "customer_support",
            "finance",
            "social_media",
            "revenue_ops",
        ),
        founder_decisions=(
            "Setting or changing price",
            "First activation of any production integration (payments, email domain, analytics)",
            "Production deployments",
            "Public launch announcements",
            "Contracts, terms, or refunds above the YELLOW limit",
            "Advancing to Marketing",
        ),
        exit_gate_name="Launched",
        exit_gate=(
            GateCriterion("mvp_live", ">=", 1, "MVP live for customers (1 = yes)"),
            GateCriterion("checkout_verified", ">=", 1, "Checkout verified in test mode and on a first live payment (1 = yes)"),
            GateCriterion("paying_customers", ">=", 3, "Paying customers"),
            GateCriterion("activation_rate", ">=", 0.4, "Share of new customers reaching first value"),
            GateCriterion("critical_open_defects", "<=", 0, "Open critical defects"),
        ),
    ),
    "marketing": PhaseSpec(
        key="marketing",
        name="Marketing",
        lead_agent="marketing",
        objective=(
            "Find at least one acquisition channel that brings customers "
            "repeatably at acceptable unit economics."
        ),
        playbook=(
            "Keep an experiment backlog scored by impact, confidence and ease.",
            "Run one or two channel experiments at a time, each with a metric and stop rule set in advance.",
            "Test messaging and positioning across landing page, outbound and content.",
            "Review the full funnel weekly: visitors, leads, trials, paid, churn.",
            "Double down on channels whose CAC payback beats the target; stop the rest.",
            "Keep paid spend at $0 until the founder approves a budget.",
        ),
        worker_agents=(
            "social_media",
            "email_outreach",
            "ads_management",
            "competitor_research",
            "code_generation",
            "finance",
            "customer_support",
            "business_planning",
        ),
        founder_decisions=(
            "Any new paid channel, or budget above the approved limit",
            "New geography or customer segment",
            "Public claims about customer results",
            "Pricing or discount changes beyond the approved offer",
            "Advancing to Scaling",
        ),
        exit_gate_name="Repeatable acquisition",
        exit_gate=(
            GateCriterion("mrr_usd", ">=", 3000, "Monthly recurring revenue (USD)"),
            GateCriterion("cac_payback_months", "<=", 6, "CAC payback on the best channel (months)"),
            GateCriterion("ltv_to_cac", ">=", 3, "Lifetime value to CAC ratio"),
            GateCriterion("consecutive_growth_weeks", ">=", 4, "Consecutive weeks of new-customer growth"),
            GateCriterion("monthly_logo_churn", "<=", 0.06, "Monthly customer churn"),
        ),
    ),
    "scaling": PhaseSpec(
        key="scaling",
        name="Scaling",
        lead_agent="scaling",
        objective=(
            "Grow while removing the founder as hidden infrastructure: better "
            "margins, lower churn, and most routine work done correctly by agents."
        ),
        playbook=(
            "Find the most repeated manual steps and automate them through tested pull requests.",
            "Improve unit economics: gross margin, CAC and retention.",
            "Expand proven channels carefully; propose (never launch) new segments.",
            "Track founder hours per $1k MRR and the share of actions agents complete without correction.",
            "Harden reliability: failure recovery, monitoring and written runbooks.",
            "Assemble keep / scale / sell evidence.",
        ),
        worker_agents=(
            "code_generation",
            "finance",
            "customer_support",
            "revenue_ops",
            "governance",
            "business_planning",
            "ads_management",
            "email_outreach",
            "social_media",
        ),
        founder_decisions=(
            "Hiring contractors or vendors",
            "New products, niches or markets",
            "Budget increases beyond thresholds",
            "Security, legal or compliance issues",
            "The keep / scale / sell decision",
        ),
        exit_gate_name="Disposition-ready",
        exit_gate=(
            GateCriterion("mrr_usd", ">=", 10000, "Monthly recurring revenue (USD)"),
            GateCriterion("gross_margin", ">=", 0.7, "Gross margin"),
            GateCriterion("monthly_logo_churn", "<=", 0.04, "Monthly customer churn"),
            GateCriterion("founder_hours_per_1k_mrr", "<=", 2, "Founder hours per $1k MRR per month"),
            GateCriterion("automated_action_share", ">=", 0.8, "Share of routine actions agents complete without correction"),
        ),
    ),
}


# Idea-scoring rubric, tuned to the Polsia thesis (an online business that
# agents can run). Weights sum to 1.0. Scores are 0-5 per criterion.
IDEA_RUBRIC: dict[str, tuple[float, str]] = {
    "pain_intensity": (0.20, "How acute and frequent the problem is for the buyer"),
    "willingness_to_pay": (0.20, "Evidence buyers already pay to solve it"),
    "online_reachability": (0.15, "Buyers can be reached through online channels agents can run"),
    "automation_fit": (0.20, "Share of ongoing fulfillment agents can do without a human"),
    "speed_to_signal": (0.10, "How fast a smoke test can produce a real demand signal"),
    "compliance_safety": (0.10, "Low regulatory, licensing and messaging risk (5 = very safe)"),
    "differentiation": (0.05, "A credible wedge against existing solutions"),
}


class LifecycleError(ValueError):
    """Raised for unknown phases, metrics, or invalid overrides."""


def get_phase(key: str) -> PhaseSpec:
    try:
        return PHASES[key]
    except KeyError as exc:
        raise LifecycleError(f"Unknown venture phase: {key!r}") from exc


def next_phase(key: str) -> str | None:
    get_phase(key)
    index = PHASE_ORDER.index(key)
    return PHASE_ORDER[index + 1] if index + 1 < len(PHASE_ORDER) else None


def is_valid_metric_name(name: object) -> bool:
    return isinstance(name, str) and bool(METRIC_NAME_RE.match(name))


def coerce_metric_value(value: object) -> float | None:
    """Numbers (and bools as 0/1) only; anything else is treated as missing."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def effective_gate(phase_key: str, overrides: dict | None = None) -> tuple[GateCriterion, ...]:
    """The phase's exit gate with founder-set threshold overrides applied.
    Only thresholds of existing criteria can change — not operators, and no
    criteria can be removed or added this way."""
    phase = get_phase(phase_key)
    if not overrides:
        return phase.exit_gate
    known = {criterion.metric for criterion in phase.exit_gate}
    unknown = set(overrides) - known
    if unknown:
        raise LifecycleError(f"Overrides name metrics not in the {phase.name} gate: {sorted(unknown)}")
    gate = []
    for criterion in phase.exit_gate:
        if criterion.metric in overrides:
            threshold = coerce_metric_value(overrides[criterion.metric])
            if threshold is None:
                raise LifecycleError(f"Override for {criterion.metric} must be a finite number")
            criterion = GateCriterion(criterion.metric, criterion.op, threshold, criterion.description)
        gate.append(criterion)
    return tuple(gate)


def validate_gate_overrides(overrides: dict | None) -> dict:
    """Overrides are stored per venture as {phase_key: {metric: threshold}}."""
    if not overrides:
        return {}
    if not isinstance(overrides, dict):
        raise LifecycleError("gate_overrides must be an object keyed by phase")
    for phase_key, phase_overrides in overrides.items():
        if not isinstance(phase_overrides, dict):
            raise LifecycleError(f"gate_overrides.{phase_key} must be an object")
        effective_gate(phase_key, phase_overrides)
    return overrides


def evaluate_gate(phase_key: str, metrics: dict | None, overrides: dict | None = None) -> dict:
    """Evaluate a phase's exit gate against recorded metrics.

    `metrics` maps metric name -> {"value": number, "verified": bool, ...}.
    Each criterion's status is one of:
      pass        verified value meets the threshold
      fail        value does not meet the threshold (verified or not)
      unverified  value would pass, but nobody has verified it yet
      missing     no usable numeric value recorded
    The gate passes only when every criterion is "pass".
    """
    phase = get_phase(phase_key)
    metrics = metrics or {}
    criteria = []
    for criterion in effective_gate(phase_key, overrides):
        entry = metrics.get(criterion.metric)
        raw = entry.get("value") if isinstance(entry, dict) else None
        value = coerce_metric_value(raw)
        verified = bool(entry.get("verified")) if isinstance(entry, dict) else False
        if value is None:
            status = "missing"
        elif not _OPS[criterion.op](value, criterion.threshold):
            status = "fail"
        elif not verified:
            status = "unverified"
        else:
            status = "pass"
        criteria.append({**criterion.as_dict(), "value": value, "verified": verified, "status": status})

    def _with(status: str) -> list[str]:
        return [c["metric"] for c in criteria if c["status"] == status]

    return {
        "phase": phase.key,
        "gate": phase.exit_gate_name,
        "passed": all(c["status"] == "pass" for c in criteria),
        "criteria": criteria,
        "failing": _with("fail"),
        "missing": _with("missing"),
        "unverified": _with("unverified"),
        "next_phase": next_phase(phase.key),
    }


def commander_dispatchable(phase_key: str) -> tuple[str, ...]:
    """Commander delegates to the current phase lead plus the shared services."""
    return (get_phase(phase_key).lead_agent, TRACKER, PREDICTOR)


def lead_dispatchable(phase_key: str) -> tuple[str, ...]:
    """A phase lead delegates only to that phase's worker agents."""
    return get_phase(phase_key).worker_agents


def score_idea(scores: dict | None) -> dict:
    """Deterministic weighted score (0-100) from 0-5 rubric scores. Missing
    or out-of-range criteria count as 0 and are listed, so an incomplete
    assessment can't look better than it is."""
    scores = scores or {}
    total = 0.0
    breakdown = {}
    missing = []
    for criterion, (weight, _description) in IDEA_RUBRIC.items():
        entry = scores.get(criterion)
        raw = entry.get("score") if isinstance(entry, dict) else entry
        value = coerce_metric_value(raw)
        if value is None or not 0 <= value <= 5:
            missing.append(criterion)
            value = 0.0
        contribution = weight * value / 5 * 100
        total += contribution
        breakdown[criterion] = {"score": value, "weight": weight, "points": round(contribution, 1)}
    return {
        "weighted_score": round(total, 1),
        "complete": not missing,
        "missing": missing,
        "criteria": breakdown,
    }


def phase_brief(phase_key: str, overrides: dict | None = None) -> str:
    """Human-readable phase brief injected into agent prompts."""
    phase = get_phase(phase_key)
    lines = [
        f"Phase: {phase.name}",
        f"Objective: {phase.objective}",
        "Playbook:",
        *[f"  {i}. {step}" for i, step in enumerate(phase.playbook, start=1)],
        f"Exit gate ({phase.exit_gate_name}):",
        *[
            f"  - {c.metric} {c.op} {c.threshold:g} — {c.description}"
            for c in effective_gate(phase_key, overrides)
        ],
        "Founder (RED) decisions in this phase:",
        *[f"  - {decision}" for decision in phase.founder_decisions],
    ]
    return "\n".join(lines)
